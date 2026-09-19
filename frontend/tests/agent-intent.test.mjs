import assert from 'node:assert/strict'
import test from 'node:test'
import { isResumeCommand, shouldResumeConversation } from '../src/agent-intent.ts'

test('continue and retry reuse existing requirements', () => {
  for (const text of ['继续', '请继续报价！', '重试', '再试一次', '继续 查价。', 'RETRY', '重新计价']) {
    assert.equal(isResumeCommand(text), true, text)
    assert.equal(shouldResumeConversation(text, 0, true), true, text)
  }
  assert.equal(shouldResumeConversation('', 0, true), true)
})

test('new facts, new files and unrecognized initial jobs still extract', () => {
  for (const text of ['继续，数量改成3件', '继续报价，增加安装费', '重新识别', '继续使用304不锈钢', 'retry with 3 items']) {
    assert.equal(shouldResumeConversation(text, 0, true), false, text)
  }
  assert.equal(shouldResumeConversation('继续', 1, true), false)
  assert.equal(shouldResumeConversation('继续', 0, false), false)
})
