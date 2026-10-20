<template>
  <div class="ui-form-field" :class="{ 'ui-form-field--invalid': hasError }">
    <label v-if="label || $slots.label" class="ui-form-field__label" :for="controlId">
      <slot name="label" :id="controlId">{{ label }}</slot>
      <span v-if="required" class="ui-form-field__required" aria-hidden="true">*</span>
    </label>

    <slot
      :id="controlId"
      :hint-id="hintId"
      :error-id="errorId"
      :described-by="describedBy"
      :invalid="hasError"
      :required="required"
      :control-attrs="controlAttrs"
    />

    <p v-if="hasHint" :id="hintId" class="ui-form-field__hint">
      <slot name="hint">{{ hint }}</slot>
    </p>
    <p v-if="hasError" :id="errorId" class="ui-form-field__error">
      <slot name="error">{{ error }}</slot>
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, useId, useSlots } from 'vue'

const props = withDefaults(
  defineProps<{
    label?: string
    for?: string
    required?: boolean
    hint?: string
    error?: string
  }>(),
  {
    label: '',
    for: '',
    required: false,
    hint: '',
    error: '',
  },
)

const slots = useSlots()
const generatedId = useId()
const controlId = computed(() => props.for || `field-${generatedId}`)
const hintId = computed(() => `${controlId.value}-hint`)
const errorId = computed(() => `${controlId.value}-error`)
const hasHint = computed(() => Boolean(props.hint || slots.hint))
const hasError = computed(() => Boolean(props.error || slots.error))
const describedBy = computed(() => {
  const ids = [hasHint.value ? hintId.value : '', hasError.value ? errorId.value : ''].filter(
    Boolean,
  )
  return ids.length > 0 ? ids.join(' ') : undefined
})
const controlAttrs = computed(() => ({
  id: controlId.value,
  'aria-describedby': describedBy.value,
  'aria-invalid': hasError.value ? 'true' : undefined,
  required: props.required || undefined,
}))
</script>

<style scoped>
.ui-form-field {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: var(--space-2);
}

.ui-form-field__label {
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  line-height: var(--leading-snug);
}

.ui-form-field__required,
.ui-form-field__error {
  color: var(--danger);
}

.ui-form-field__required {
  margin-inline-start: var(--space-1);
}

.ui-form-field__hint,
.ui-form-field__error {
  margin: var(--space-0);
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
}

.ui-form-field__hint {
  color: var(--text-muted);
}

.ui-form-field__error {
  font-size: var(--text-md);
  overflow-wrap: anywhere;
}
</style>
