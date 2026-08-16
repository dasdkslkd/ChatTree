// 解析后端透传的 "USER MENTIONED FILES" 前缀块：返回附件文件名与去掉前缀后的正文。
// MainPage / TreeView / slashRuntime 共用同一份实现（P17 收敛三处重复正则）。
export function parseFileMention(content: string): { fileNames: string[]; cleanContent: string } | null {
  const match = content.match(/^'''USER MENTIONED FILES:\s+(.*?)\s+'''\u000A\u000A[\s\S]*?\u000A---\u000A\u000A/s);
  if (!match) return null;
  const fileNames = match[1].split(/\s+/).filter(Boolean);
  const cleanContent = content.slice(match[0].length);
  return { fileNames, cleanContent };
}

export function stripFileMention(content: string): string {
  return parseFileMention(content)?.cleanContent ?? content;
}