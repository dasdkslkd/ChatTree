import './App.css'
import { cn } from '@/lib/utils'
import { Toaster } from '@/components/ui/sonner'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { MessageSquare, StickyNote, Settings } from 'lucide-react'
import ChatPage from './pages/MainPage'
import SettingsPage from './pages/SettingsPage'
import PromptsPage from './pages/PromptsPage'
import { useNavigationStore } from './store/navigationStore'

type PageType = 'chat' | 'prompts' | 'settings';

const navItems: { key: PageType; label: string; icon: typeof MessageSquare }[] = [
  { key: 'chat', label: '聊天', icon: MessageSquare },
  { key: 'prompts', label: '提示词', icon: StickyNote },
  { key: 'settings', label: '设置', icon: Settings },
];

function App() {
  const { currentPage, setCurrentPage } = useNavigationStore();

  return (
    <TooltipProvider>
      <div className="flex h-screen w-screen">
        {/* 侧边导航栏 */}
        <nav className="w-12 bg-muted border-r flex flex-col items-center pt-2 gap-1">
          {navItems.map((item) => {
            const isActive = currentPage === item.key;
            const Icon = item.icon;
            return (
              <Tooltip key={item.key}>
                <TooltipTrigger asChild>
                  <div
                    className={cn(
                      'w-10 h-10 flex items-center justify-center rounded-lg cursor-pointer text-muted-foreground transition-colors hover:bg-background hover:text-foreground',
                      isActive && 'bg-background text-foreground shadow-sm'
                    )}
                    onClick={() => setCurrentPage(item.key)}
                  >
                    <Icon size={20} />
                  </div>
                </TooltipTrigger>
                <TooltipContent side="right">{item.label}</TooltipContent>
              </Tooltip>
            );
          })}
        </nav>

        {/* 主内容区 */}
        <div className="flex-1 overflow-hidden">
          <div className={cn('h-full w-full', currentPage !== 'chat' && 'hidden')}>
            <ChatPage />
          </div>
          <div className={cn('h-full w-full', currentPage !== 'prompts' && 'hidden')}>
            <PromptsPage />
          </div>
          <div className={cn('h-full w-full', currentPage !== 'settings' && 'hidden')}>
            <SettingsPage />
          </div>
        </div>
      </div>
      <Toaster />
    </TooltipProvider>
  );
}

export default App
