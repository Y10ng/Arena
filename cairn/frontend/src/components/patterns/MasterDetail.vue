<template>
  <div
    class="master-detail"
    :class="{
      'configuration-workspace': configuration,
      'master-detail--fill-height': fillHeight,
    }"
  >
    <aside class="md-master">
      <slot name="master" />
    </aside>
    <section class="md-detail">
      <slot name="detail" />
    </section>
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ configuration?: boolean; fillHeight?: boolean }>(), {
  configuration: false,
  fillHeight: false,
})
</script>

<style scoped>
.master-detail {
  display: grid;
  grid-template-columns: var(--master-detail-master-width, 270px) minmax(0, 1fr);
  gap: var(--space-4);
  align-items: start;
  min-width: 0;
}

.md-master,
.md-detail {
  min-width: 0;
}

.master-detail--fill-height {
  grid-template-rows: minmax(0, 1fr);
  height: 100%;
  min-height: 0;
  align-items: stretch;
  overflow: hidden;
}

.master-detail--fill-height > .md-master,
.master-detail--fill-height > .md-detail {
  min-height: 0;
  overflow: hidden;
}

.md-master {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.md-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.master-detail.configuration-workspace {
  --master-detail-master-width: minmax(300px, 340px);
}

@media (max-width: 900px) {
  .master-detail.configuration-workspace {
    grid-template-columns: 1fr;
  }

  .master-detail--fill-height.configuration-workspace {
    grid-template-rows: auto;
    height: auto;
    align-items: start;
    overflow: visible;
  }

  .master-detail--fill-height.configuration-workspace > .md-master,
  .master-detail--fill-height.configuration-workspace > .md-detail {
    overflow: visible;
  }
}

@media (max-width: 768px) {
  .master-detail:not(.configuration-workspace) {
    grid-template-columns: 1fr;
  }

  .master-detail--fill-height:not(.configuration-workspace) {
    grid-template-rows: auto;
    height: auto;
    align-items: start;
    overflow: visible;
  }

  .master-detail--fill-height:not(.configuration-workspace) > .md-master,
  .master-detail--fill-height:not(.configuration-workspace) > .md-detail {
    overflow: visible;
  }
}
</style>
