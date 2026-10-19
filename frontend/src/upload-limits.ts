import { reactive } from 'vue'

export const uploadLimits = reactive({
  max_files: 6, max_file_bytes: 20 * 1024 * 1024, max_pdf_bytes: 200 * 1024 * 1024,
  max_job_bytes: 500 * 1024 * 1024, max_pages: 500, max_lines: 10000,
})
export const attachmentAccept = '.jpg,.jpeg,.png,.webp,.pdf,.xlsx,.xls,.csv,.tsv,.txt,.docx'

export function validateAttachments(files: { name: string; size: number }[], existingCount = 0, existingBytes = 0): string {
  if (files.length + existingCount > uploadLimits.max_files) return `每个会话累计最多 ${uploadLimits.max_files} 个文件`
  if (files.some(file => !/\.(png|jpe?g|webp|pdf|xlsx|xls|csv|tsv|txt|docx)$/i.test(file.name))) {
    return '支持图片、PDF、XLSX、XLS、CSV、TSV、TXT和DOCX'
  }
  for (const file of files) {
    const limit = /\.pdf$/i.test(file.name) ? uploadLimits.max_pdf_bytes : uploadLimits.max_file_bytes
    if (file.size > limit) return `${file.name} 不能超过 ${limit / 1024 / 1024}MB`
  }
  if (files.reduce((sum, file) => sum + file.size, existingBytes) > uploadLimits.max_job_bytes) {
    return `会话文件合计不能超过 ${uploadLimits.max_job_bytes / 1024 / 1024}MB`
  }
  return ''
}
