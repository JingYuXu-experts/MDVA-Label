export interface TrainingProgress {
  phase: string
  state: string
  headline: string
  next_step: string
  completed_runs: number
  total_runs: number
  completed_percent: number
  automatic_continuation: boolean
  next_run_id?: string | null
  coordinator: { updated_utc?: string; reason?: string; state?: string }
  active?: { id: string; completed_epoch: number; active_epoch?: number; images_processed?: number; train_images?: number } | null
  last_run?: { id: string; state: string; completed_epoch: number; best_validation_dice?: number; updated_utc?: string; error?: string } | null
  checkpoint?: { exists: boolean; filename: string; completed_epoch: number; saved_utc?: string | null } | null
  epoch_budget?: { completed: number; maximum: number; minimum: number; patience: number; current_percent: number; recent_epoch_seconds?: number | null; earliest_additional_epochs_if_no_improvement: number; remaining_upper_seconds?: number | null } | null
  next_commands: { label: string; command: string }[]
}
export interface JournalEntry {
  id: string
  recorded_utc: string
  kind: string
  title: string
  message: string
  run_id?: string
  epoch?: number
}
export interface TrainingFile {
  id: string
  title: string
  description: string
  filename: string
  available: boolean
  size: number
  updated_utc?: string | null
}
export interface FilePreview extends TrainingFile { text: string; truncated: boolean }
