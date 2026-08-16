import {
  CircleCheckIcon,
  InfoIcon,
  Loader2Icon,
  OctagonXIcon,
  TriangleAlertIcon,
} from "lucide-react"
import { Toaster as Sonner, toast, type ToasterProps } from "sonner"
import { useThemeStore } from "@/store/themeStore"

// 移开鼠标：重新触发生命周期动画，让「停留 1s + 渐变 1s」从头再走一遍（移开后依然 2s 消失）
const restartToastAnimation = (element: HTMLElement) => {
  element.style.animation = "none"
  void element.offsetWidth
  element.style.animation = ""
}

const Toaster = ({ ...props }: ToasterProps) => {
  const resolvedTheme = useThemeStore((state) => state.resolvedTheme)

  const handleToastOut = (event: React.MouseEvent<HTMLElement>) => {
    const el = (event.target as HTMLElement).closest("[data-sonner-toast]") as HTMLElement | null
    const id = el?.getAttribute("data-testid")
    if (!el || !id || el.dataset.type === "loading") return
    if (event.relatedTarget instanceof Node && el.contains(event.relatedTarget)) return
    restartToastAnimation(el)
  }

  // 生命周期动画结束（渐变到透明）后真正移除 toast；loading 等无动画类型不会触发
  const handleToastAnimationEnd = (event: React.AnimationEvent<HTMLElement>) => {
    if (event.animationName !== "sonner-toast-auto-fade") return
    const el = (event.target as HTMLElement).closest("[data-sonner-toast]")
    const id = el?.getAttribute("data-testid")
    if (el && id) toast.dismiss(id)
  }

  return (
    <div onMouseOut={handleToastOut} onAnimationEnd={handleToastAnimationEnd}>
      <Sonner
        theme={resolvedTheme}
        duration={2000}
        toastOptions={{ closeButton: true }}
        className="toaster group"
        icons={{
          success: <CircleCheckIcon className="size-4" />,
          info: <InfoIcon className="size-4" />,
          warning: <TriangleAlertIcon className="size-4" />,
          error: <OctagonXIcon className="size-4" />,
          loading: <Loader2Icon className="size-4 animate-spin" />,
        }}
        style={
          {
            "--normal-bg": "var(--popover)",
            "--normal-text": "var(--popover-foreground)",
            "--normal-border": "var(--border)",
            "--border-radius": "var(--radius)",
          } as React.CSSProperties
        }
        {...props}
      />
    </div>
  )
}

export { Toaster }
