import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusableElements(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((element) => (
    element.getAttribute('aria-hidden') !== 'true'
    && (element.offsetWidth > 0 || element.offsetHeight > 0 || element.getClientRects().length > 0)
  ))
}

function isTopmostModal(dialog: HTMLElement): boolean {
  const modals = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]'))
    .filter((element) => element.offsetWidth > 0 || element.offsetHeight > 0 || element.getClientRects().length > 0)
  return modals[modals.length - 1] === dialog
}

export function restoreModalTrigger(trigger: HTMLElement | null | (() => HTMLElement | null)) {
  window.requestAnimationFrame(() => {
    const target = typeof trigger === 'function' ? trigger() : trigger
    target?.focus()
  })
}

export function useModalFocus({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLElement>(null)
  const closeRef = useRef(onClose)

  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!open) return
    const frame = window.requestAnimationFrame(() => {
      const dialog = dialogRef.current
      if (!dialog) return
      const target = focusableElements(dialog)[0] || dialog
      target.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [open])

  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    const dialog = dialogRef.current
    const origin = event.target instanceof Element ? event.target : null
    if (!dialog || !isTopmostModal(dialog) || event.defaultPrevented || origin?.closest('[role="dialog"]') !== dialog) return

    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      closeRef.current()
      return
    }
    if (event.key !== 'Tab') return

    event.stopPropagation()
    const elements = focusableElements(dialog)
    if (!elements.length) {
      event.preventDefault()
      dialog.focus()
      return
    }
    const first = elements[0]
    const last = elements[elements.length - 1]
    const current = document.activeElement
    const currentIsFocusable = current instanceof HTMLElement && elements.includes(current)
    if (event.shiftKey && (current === first || !currentIsFocusable)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (current === last || !currentIsFocusable)) {
      event.preventDefault()
      first.focus()
    }
  }

  return { dialogRef, onKeyDown }
}
