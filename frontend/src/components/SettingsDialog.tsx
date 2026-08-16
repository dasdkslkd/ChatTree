import { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';
import { TextTooltip } from '@/components/ui/text-tooltip';
import {
  Dialog,
  DialogContent,
} from '@/components/ui/dialog';
import {
  Settings, StickyNote, Server, Wrench, Link2,
  Sparkles, Bot, Package, MessageSquare, FolderOpen, Terminal, HardDrive, Coins, Brain, Palette,
} from 'lucide-react';
import { useNavigationStore, type SettingsSection } from '../store/navigationStore';
import { AppearanceSection } from './settings/sections/appearance';
import { SshHostsSection } from './settings/sections/ssh';
import { ProvidersSection } from './settings/sections/providers';
import { ProjectsSection } from './settings/sections/projects';
import { CapabilitiesSection } from './settings/sections/capabilities';
import { BuiltinToolsSection } from './settings/sections/builtin_tools';
import { DevEnvironmentSection } from './settings/sections/dev_environment';
import { McpSection } from './settings/sections/mcp';
import { PromptsSection } from './settings/sections/prompts';
import { StorageSection } from './settings/sections/storage';
import { UsageStatsSection } from './settings/sections/usage_stats';
import { MemorySection } from './settings/sections/memory';

const SETTINGS_NAV: { key: SettingsSection; label: string; icon: typeof Settings; group: string }[] = [
  { key: 'appearance', label: '外观', icon: Palette, group: '应用' },
  { key: 'providers', label: '供应商', icon: Server, group: '应用' },
  { key: 'projects', label: '项目', icon: FolderOpen, group: '应用' },
  { key: 'memory', label: '记忆', icon: Brain, group: '应用' },
  { key: 'ssh', label: 'SSH Hosts', icon: Link2, group: '应用' },
  { key: 'builtin_tools', label: '内置工具', icon: Wrench, group: '工具与能力' },
  { key: 'dev_environment', label: '开发环境', icon: Terminal, group: '工具与能力' },
  { key: 'skills', label: 'Skill', icon: Sparkles, group: '工具与能力' },
  { key: 'mcp', label: 'MCP', icon: Link2, group: '工具与能力' },
  { key: 'agents', label: 'Agent', icon: Bot, group: '工具与能力' },
  { key: 'plugins', label: '插件', icon: Package, group: '工具与能力' },
  { key: 'prompts', label: '提示词', icon: StickyNote, group: '应用' },
  { key: 'storage', label: '存储', icon: HardDrive, group: '应用' },
  { key: 'usage_stats', label: '用量统计', icon: Coins, group: '应用' },
];

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultSection?: SettingsSection;
}

export function SettingsPageView({ defaultSection = 'providers' }: { defaultSection?: SettingsSection }) {
  const [section, setSection] = useState<SettingsSection>(defaultSection);
  const { openChat } = useNavigationStore();

  useEffect(() => {
    setSection(defaultSection);
  }, [defaultSection]);

  return (
    <div className="flex h-full overflow-hidden" style={{ background: 'var(--bg-surface)' }}>
      {/* Left nav */}
      <nav
        className="app-sidebar flex-shrink-0"
        style={{ width: '200px' }}
      >
        <div className="app-sidebar-topbar">
          <span className="text-base font-semibold" style={{ color: 'var(--fg-85)' }}>设置</span>
        </div>

        <div className="flex-1 overflow-y-auto p-2 custom-scrollbar">
          {(() => {
            const groups: Record<string, typeof SETTINGS_NAV> = {};
            SETTINGS_NAV.forEach(item => {
              if (!groups[item.group]) groups[item.group] = [];
              groups[item.group].push(item);
            });
            return Object.entries(groups).map(([group, items]) => (
              <div key={group}>
                <div
                  className="app-sidebar-project-heading"
                  style={{ letterSpacing: '0.025em' }}
                >
                  {group}
                </div>
                <div className="flex flex-col gap-1">
                  {items.map(item => {
                    const Icon = item.icon;
                    const isActive = section === item.key;
                    return (
                      <button
                        type="button"
                        key={item.key}
                        className={cn('app-sidebar-action', isActive && 'is-active')}
                        onClick={() => setSection(item.key)}
                      >
                        <Icon
                          className="w-4 h-4 flex-shrink-0"
                          style={{ color: isActive ? 'var(--icon-accent)' : 'var(--icon-tertiary)' }}
                        />
                        <span>{item.label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ));
          })()}
        </div>
        <div className="app-sidebar-footer">
          <TextTooltip content="返回对话">
            <button
              type="button"
              className="app-sidebar-action"
              onClick={openChat}
            >
              <MessageSquare className="h-4 w-4 shrink-0" />
              <span>返回对话</span>
            </button>
          </TextTooltip>
        </div>
      </nav>

      {/* Content */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {section === 'appearance' && <AppearanceSection />}
        {section === 'providers' && <ProvidersSection />}
        {section === 'projects' && <ProjectsSection />}
        {section === 'memory' && <MemorySection />}
        {section === 'ssh' && <SshHostsSection />}
        {section === 'builtin_tools' && <BuiltinToolsSection />}
        {section === 'dev_environment' && <DevEnvironmentSection />}
        {section === 'skills' && <CapabilitiesSection view="skills" />}
        {section === 'mcp' && <McpSection />}
        {section === 'agents' && <CapabilitiesSection view="agents" />}
        {section === 'plugins' && <CapabilitiesSection view="plugins" />}
        {section === 'prompts' && <PromptsSection />}
        {section === 'storage' && <StorageSection />}
        {section === 'usage_stats' && <UsageStatsSection />}
      </div>
    </div>
  );
}

export function SettingsDialog({ open, onOpenChange, defaultSection = 'providers' }: SettingsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="p-0 gap-0 overflow-hidden border-0"
        style={{
          width: 'min(760px, 94%)',
          height: 'min(540px, 88%)',
          background: 'var(--bg-elevated)',
          border: '0.5px solid var(--border)',
          borderRadius: 'var(--radius-2xl)',
          boxShadow: 'var(--shadow-2xl)',
        }}
      >
        <SettingsPageView defaultSection={defaultSection} />
      </DialogContent>
    </Dialog>
  );
}
