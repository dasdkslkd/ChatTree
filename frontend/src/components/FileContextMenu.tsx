import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';

export interface ContextMenuItem {
  key: string;
  label: string;
  icon?: ReactNode;
  disabled?: boolean;
  destructive?: boolean;
  separatorBefore?: boolean;
  onSelect: () => void;
}

interface FileContextMenuProps {
  x: number;
  y: number;
  items: ContextMenuItem[];
  onClose: () => void;
}

/**
 * 轻量可扩展的右键菜单浮层（受控）。
 * 通过 items 数组声明菜单项，支持 icon / disabled / destructive / 菜单内分组分隔线。
 * 点击菜单项、遮罩、Escape 或再次右键均会关闭。
 */
export function FileContextMenu({ x, y, items, onClose }: FileContextMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ x, y });

  // 定位到鼠标处，超出视口时向上/向左回退
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { innerWidth, innerHeight } = window;
    const rect = el.getBoundingClientRect();
    setPos({
      x: x + rect.width > innerWidth ? Math.max(4, innerWidth - rect.width) : x,
      y: y + rect.height > innerHeight ? Math.max(4, innerHeight - rect.height) : y,
    });
  }, [x, y]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50"
      onMouseDown={onClose}
      onContextMenu={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <div
        ref={ref}
        role="menu"
        className="min-w-[10rem] rounded-md border bg-popover p-1 shadow-md"
        style={{ borderColor: 'var(--border)', position: 'fixed', left: pos.x, top: pos.y }}
        onMouseDown={(event) => event.stopPropagation()}
        onContextMenu={(event) => {
          event.preventDefault();
          event.stopPropagation();
        }}
      >
        {items.map((item) => (
          <button
            key={item.key}
            type="button"
            role="menuitem"
            disabled={item.disabled}
            onClick={() => {
              onClose();
              item.onSelect();
            }}
            className="relative flex w-full cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-hidden select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 enabled:hover:bg-accent"
            style={{
              color: item.destructive ? 'var(--accent-red)' : 'var(--fg-85)',
              borderTop: item.separatorBefore ? `1px solid var(--border)` : undefined,
              marginTop: item.separatorBefore ? 2 : undefined,
            }}
          >
            <span className="inline-flex size-4 shrink-0 items-center justify-center [&>svg]:text-current">
              {item.icon}
            </span>
            <span className="truncate">{item.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}