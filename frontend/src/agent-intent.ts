const resumeCommands = new Set([
  '继续', '请继续', '继续报价', '请继续报价', '继续查价', '请继续查价',
  '继续生成报价', '重试', '请重试', '重试报价', '再试一次', '重新尝试',
  '恢复报价', '接着报价', '重新报价', '重新计价', '重新计算报价',
  'continue', 'resume', 'retry',
])

export function isResumeCommand(text: string): boolean {
  return resumeCommands.has(text.replace(/[\s。.!！?？,，;；]+/g, '').toLowerCase())
}

export function shouldResumeConversation(text: string, fileCount: number, hasRequirements: boolean): boolean {
  return hasRequirements && fileCount === 0 && (!text.trim() || isResumeCommand(text))
}
