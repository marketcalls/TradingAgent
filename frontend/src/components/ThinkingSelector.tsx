/** Thinking control, shown only when the configured model can actually reason.
 *
 * Whether a model reasons is detected per model by the backend, not per provider:
 * openai/gpt-4o cannot and openai/o3 can. When it cannot, this renders nothing at all
 * rather than offering a switch that would do nothing.
 *
 * The shape of the control follows the model too. Ollama exposes thinking as a boolean,
 * so it gets Off/On. A model with graded effort gets the full scale. A model that
 * reasons but cannot be silenced (DeepSeek V4 Flash, measured) simply has no Off.
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

  const options = reasoning.supported
  const title = [
    reasoning.note,
    reasoning.detectedBy ? `Detected via ${reasoning.detectedBy}.` : "",
    !reasoning.canDisable ? "This model cannot stop reasoning entirely." : "",
  ]
    .filter(Boolean)
    .join(" ")

  return (
    <div className="flex items-center gap-1.5" title={title}>
      <span className="text-xs text-muted">Thinking</span>
      <div className="flex overflow-hidden rounded-lg border border-subtle">
        {options.map((option) => {
          const active = value === option
          return (
            <button
              key={option}
              type="button"
              disabled={disabled}
              onClick={() => onChange(option)}
              aria-pressed={active}
              className={[
                "px-2 py-0.5 text-xs transition-colors",
                active ? "bg-accent text-white" : "text-muted hover:text-fg",
                disabled ? "cursor-not-allowed opacity-50" : "",
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
