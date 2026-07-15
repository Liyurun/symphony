import { useCallback, useEffect, useRef } from 'react'

interface ResizerProps {
  direction: 'horizontal' | 'vertical'
  onResize: (delta: number) => void
  className?: string
}

export default function Resizer({ direction, onResize, className = '' }: ResizerProps) {
  const isDraggingRef = useRef(false)
  const startPosRef = useRef(0)

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      isDraggingRef.current = true
      startPosRef.current = direction === 'horizontal' ? e.clientX : e.clientY
      document.body.style.cursor = direction === 'horizontal' ? 'col-resize' : 'row-resize'
      document.body.style.userSelect = 'none'
    },
    [direction],
  )

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!isDraggingRef.current) return
      const currentPos = direction === 'horizontal' ? e.clientX : e.clientY
      const delta = currentPos - startPosRef.current
      startPosRef.current = currentPos
      onResize(delta)
    }

    function onMouseUp() {
      isDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }

    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [direction, onResize])

  const baseClass =
    direction === 'horizontal'
      ? 'w-1 cursor-col-resize hover:bg-ctp-mauve/40 bg-transparent transition-colors shrink-0'
      : 'h-1 cursor-row-resize hover:bg-ctp-mauve/40 bg-transparent transition-colors shrink-0'

  return <div className={`${baseClass} ${className}`} onMouseDown={onMouseDown} />
}
