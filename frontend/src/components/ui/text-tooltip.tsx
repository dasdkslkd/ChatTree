"use client"

import * as React from "react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

type TextTooltipProps = {
  content?: React.ReactNode
  children: React.ReactElement
  side?: "top" | "right" | "bottom" | "left"
  align?: "start" | "center" | "end"
  className?: string
  disabled?: boolean
}

function TextTooltip({
  content,
  children,
  side,
  align,
  className,
  disabled = false,
}: TextTooltipProps) {
  if (disabled || content === null || content === undefined || content === "") {
    return children
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side} align={align} className={className}>
        {content}
      </TooltipContent>
    </Tooltip>
  )
}

export { TextTooltip }
