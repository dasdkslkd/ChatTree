# chat/compact.py - Claude Code 风格上下文压缩
import re
from math import floor
from typing import Any, Dict, List, Optional


COMPACT_MAX_OUTPUT_TOKENS = 20_000
AUTO_COMPACT_RATIO = 0.9
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
MICROCOMPACT_MAX_TOOL_CONTENT_CHARS = 8_000
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_MAX_CHARS_PER_FILE = 20_000

MENTIONED_FILES_RE = re.compile(r"^'''USER MENTIONED FILES:\s+(.*?)\s+'''\n\n", re.S)

NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn - you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

DETAILED_ANALYSIS_INSTRUCTION = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like file names, code snippets, function signatures, and file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

BASE_COMPACT_PROMPT = f"""Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

{DETAILED_ANALYSIS_INSTRUCTION}

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include important code snippets where applicable and explain why each file read or edit matters.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the user's feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant.
9. Optional Next Step: List the next step that is directly related to the user's most recent explicit request. If there is a next step, include direct quotes from the most recent conversation showing exactly where the work left off.

Please provide your summary using this structure:

<analysis>
[Your private drafting notes]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]

3. Files and Code Sections:
   - [File Name]
      - [Why it matters]
      - [Changes or important snippets]

4. Errors and fixes:
   - [Error and fix]

5. Problem Solving:
   [Description]

6. All user messages:
   - [User message]

7. Pending Tasks:
   - [Task]

8. Current Work:
   [Precise description]

9. Optional Next Step:
   [Next step]
</summary>
"""

NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only - "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)


def get_compact_prompt(custom_instructions: Optional[str] = None) -> str:
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions.strip()}"
    return prompt + NO_TOOLS_TRAILER


def format_compact_summary(summary: str) -> str:
    formatted = re.sub(r"<analysis>[\s\S]*?</analysis>", "", summary).strip()
    match = re.search(r"<summary>([\s\S]*?)</summary>", formatted)
    if match:
        formatted = "Summary:\n" + match.group(1).strip()
    formatted = re.sub(r"\n\n+", "\n\n", formatted)
    return formatted.strip()


def get_compact_user_summary_message(
    summary: str,
    suppress_follow_up_questions: bool = True,
    transcript_path: Optional[str] = None,
    recent_messages_preserved: bool = False,
) -> str:
    base = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        f"{format_compact_summary(summary)}"
    )
    if transcript_path:
        base += (
            "\n\nIf you need specific details from before compaction, read the full "
            f"transcript at: {transcript_path}"
        )
    if recent_messages_preserved:
        base += "\n\nRecent messages are preserved verbatim."
    if suppress_follow_up_questions:
        base += (
            "\n\nContinue the conversation from where it left off without asking the user "
            "any further questions. Resume directly - do not acknowledge the summary, "
            "do not recap what was happening, do not preface with \"I'll continue\" or similar."
        )
    return base


def microcompact_messages(
    messages: List[Dict[str, Any]],
    max_tool_content_chars: int = MICROCOMPACT_MAX_TOOL_CONTENT_CHARS,
) -> List[Dict[str, Any]]:
    """确定性瘦身：压缩过大的 tool 结果，保留用户/助手正文原样。"""
    compacted: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role != "tool" or len(content) <= max_tool_content_chars:
            compacted.append(dict(message))
            continue

        head_chars = max(max_tool_content_chars - 300, 0)
        compacted_message = dict(message)
        compacted_message["content"] = (
            content[:head_chars].rstrip()
            + f"\n\n[microcompact] Tool result truncated from {len(content)} chars "
            f"to {head_chars} chars for model context. Use the original transcript/tool result storage for full details."
        )
        compacted.append(compacted_message)
    return compacted


def extract_mentioned_import_filenames(messages: List[Dict[str, Any]], max_files: int = POST_COMPACT_MAX_FILES_TO_RESTORE) -> List[str]:
    filenames: List[str] = []
    seen = set()

    def add_filename(filename: Any) -> bool:
        if not isinstance(filename, str) or not filename:
            return False
        if filename in seen:
            return False
        seen.add(filename)
        filenames.append(filename)
        return len(filenames) >= max_files

    for message in reversed(messages):
        if message.get("role") not in ("user", "Role.USER"):
            continue
        import_files = message.get("import_files") or []
        if isinstance(import_files, list):
            for file_ref in reversed(import_files):
                filename = file_ref.get("filename") if isinstance(file_ref, dict) else file_ref
                if add_filename(filename):
                    return list(reversed(filenames))
        content = str(message.get("content") or "")
        match = MENTIONED_FILES_RE.match(content)
        if not match:
            continue
        for filename in reversed(match.group(1).split()):
            if add_filename(filename):
                return list(reversed(filenames))
    return list(reversed(filenames))


def format_restored_file_context(restored_files: List[Dict[str, Any]]) -> str:
    parts = ["Restored file context from before compaction:"]
    for file_info in restored_files:
        filename = file_info.get("filename") or "unknown"
        content = str(file_info.get("content") or "")
        truncated = " (truncated)" if file_info.get("truncated") else ""
        parts.append(f"\n--- {filename}{truncated} ---\n{content}")
    return "\n".join(parts).strip()


def get_auto_compact_threshold(context_window: int, max_output_tokens: Optional[int] = None) -> int:
    _ = max_output_tokens
    return max(floor(int(context_window or 0) * AUTO_COMPACT_RATIO), 0)
