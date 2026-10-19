import assert from 'node:assert/strict'
import test from 'node:test'
import { validateAttachments } from '../src/upload-limits.ts'

test('PDFs accept the larger limit while other files retain their limit', () => {
  assert.equal(validateAttachments([{ name: 'drawing.PDF', size: 200 * 1024 * 1024 }]), '')
  assert.match(validateAttachments([{ name: 'drawing.pdf', size: 200 * 1024 * 1024 + 1 }]), /200MB/)
  assert.match(validateAttachments([{ name: 'drawing.png', size: 21 * 1024 * 1024 }]), /20MB/)
})

test('file count and total bytes include existing attachments', () => {
  assert.match(validateAttachments([{ name: 'new.pdf', size: 1 }], 6), /6/)
  assert.match(validateAttachments([{ name: 'new.pdf', size: 100 * 1024 * 1024 }], 3, 450 * 1024 * 1024), /500MB/)
  assert.match(validateAttachments([{ name: 'bad.exe', size: 1 }]), /PDF/)
})

test('document and legacy table formats are accepted while macros remain excluded', () => {
  for (const extension of ['csv', 'tsv', 'txt', 'docx', 'xls', 'xlsx']) {
    assert.equal(validateAttachments([{ name: `source.${extension}`, size: 100 }]), '')
  }
  assert.notEqual(validateAttachments([{ name: 'source.xlsm', size: 100 }]), '')
  assert.notEqual(validateAttachments([{ name: 'source.docm', size: 100 }]), '')
})
