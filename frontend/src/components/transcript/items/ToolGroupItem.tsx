import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';

export function ToolGroupItem({ item }: { item: TranscriptItem }) {
  const [collapsed, setCollapsed] = useState(false);
  const text = getItemText(item, 'Tool activity');
  const status = getStatusText(item);
  const toolName = typeof item.props?.name === 'string' ? item.props.name : 'tool';

  return (
    <div className="w-full flex flex-col items-start" role="listitem">
      <div className={cn('tool-group', collapsed && 'collapsed')}>
        <button
          type="button"
          className="tool-group-header"
          aria-expanded={!collapsed}
          onClick={() => setCollapsed((value) => !value)}
        >
          <ChevronRight className="tg-chevron" />
          <span>工具调用</span>
          <span className="tg-count">1 个</span>
        </button>
        <div className="tool-group-body">
          <div className="tool-call">
            <button type="button" className="tc-header" aria-expanded="false">
              <ChevronRight className="tc-chevron" />
              <span className="tc-name">{toolName}</span>
              <span className="tc-summary">{text}</span>
              {status && <span className="tc-duration">{status}</span>}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
