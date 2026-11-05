import assert from 'node:assert/strict';
import test from 'node:test';
import { createProjectTools } from '../dist/project-tools.js';

test('发布合同接受真实来源引用与普通Markdown，拒绝旧来源时间参数', () => {
  const tool = createProjectTools({ allowed_tools: ['publish_custom_markdown'] }, () => {})[0];
  const reference = { method: 'read_conversation_attachment', params: { attachment_id: 'att_fixture', offset: 0, chunk_size: 20 }, expected_version: 'a'.repeat(64) };
  const value = { title: '合成成品', content: '# 合成', source_references: [reference], source_notes: ['未核验声明'], limitations: ['非完整材料'] };
  assert.deepEqual(tool.prepareArguments(value), value);
  assert.deepEqual(tool.prepareArguments({ title: '普通', content: '# 普通' }), { title: '普通', content: '# 普通' });
  assert.throws(() => tool.prepareArguments({ title: '旧参数', content: '# 旧', sources: ['伪造'], read_at: '2001-01-01T00:00:00Z' }));
  assert.throws(() => tool.prepareArguments({ ...value, source_references: [{ ...reference, method: 'read_research_session' }] }));
  assert.throws(() => tool.prepareArguments({ ...value, source_references: [{ ...reference, expected_version: 'invented' }] }));
});

test('选定工作文件可登记、按来源取回并引用原字节版本', () => {
  const tools = createProjectTools({ allowed_tools: ['register_conversation_material', 'read_conversation_research', 'publish_custom_markdown'] }, () => {});
  const tool = name => tools.find(item => item.name === name);
  const selected = { workspace_path: 'results/sample.bin' };
  assert.deepEqual(tool('register_conversation_material').prepareArguments(selected), selected);
  const read = { conversation_id: 'conv_previous', task_run_id: 'run_previous', source: 'artifacts', artifact_id: 'art_sample', workspace_path: 'inputs/original.bin' };
  assert.deepEqual(tool('read_conversation_research').prepareArguments(read), read);
  const { workspace_path, ...params } = read;
  const publish = { title: '原件来源', content: '# 仅引用', source_references: [{ method: '_read_conversation_research_bytes', params, expected_version: 'a'.repeat(64) }] };
  assert.deepEqual(tool('publish_custom_markdown').prepareArguments(publish), publish);
  assert.throws(() => tool('register_conversation_material').prepareArguments({ ...selected, conversation_id: 'conv_other' }));
});
