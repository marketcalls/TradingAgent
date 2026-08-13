/** Thinking control, shown only when the configured model can actually reason.
 *
 * Whether a model reasons is detected per model by the backend, not per provider:
 * openai/gpt-4o cannot and openai/o3 can. When it cannot, this renders nothing at all
 * rather than offering a switch that would do nothing.
 *
 * The shape of the control follows the model too. Ollama exposes thinking as a boolean,
 * so it gets Off/On. A model with graded effort gets the full scale. A model that
 * reasons but cannot be silenced (DeepSeek V4 Flash, measured) simply has no Off.
 *
 * Colours use the project's own tokens. An earlier version used text-fg, border-subtle
 * and bg-accent, none of which exist here, so Tailwind dropped them and the control was
 * invisible in both themes. The text token is muted-foreground; muted on its own is a
 * 4%-alpha BACKGROUND colour and is unreadable as text.
 */

import type { ReasoningInfo } from "../lib/api"

interface ThinkingSelectorProps {
  reasoning: ReasoningInfo | null
  value: string
  onChange: (value: string) => void
  disabled?: boolean
}

export default function ThinkingSelector({
  reasoning,
  value,
  onChange,
  disabled = false,
}: ThinkingSelectorProps) {
  // Nothing to control: not a reasoning model.
  if (!reasoning || !reasoning.modelThinks || reasoning.supported.length === 0) return null

  const title = [
    reasoning.note,
    reasoning.detectedBy ? `Detected via ${reasoning.detectedBy}.` : "",
    !reasoning.canDisable ? "This model cannot stop reasoning entirely." : "",
  ]
    .filter(Boolean)
    .join(" ")

  return (
    <div className="flex shrink-0 items-center gap-2" title={title}>
      <span className="text-xs font-medium text-muted-foreground">Thinking</span>
      <div className="flex overflow-hidden rounded-lg border border-border">
        {reasoning.supported.map((option) => {
          const active = value === option
          return (
            <button
              key={option}
              type="button"
              disabled={disabled}
              onClick={() => onChange(option)}
              aria-pressed={active}
              className={[
                "px-2.5 py-1 text-xs transition-colors",
                active
                  ? "bg-primary font-medium text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
                disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
              ].join(" ")}
            >
              {reasoning.labels[option] ?? option}
            </button>
          )
        })}
      </div>
    </div>
  )
}
