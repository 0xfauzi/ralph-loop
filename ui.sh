#!/usr/bin/env bash
# =============================================================================
# ui.sh - Ralph UI Toolkit (gum optional)
# =============================================================================
# Goals:
# - Make CLI output easy to scan with clear hierarchy and demarcations
#   (AI output vs user prompts vs system output).
# - Use `gum` for nicer styling when available.
# - Fall back to plain output (still well-structured) when `gum` is missing or
#   when not running in a TTY.
#
# Notes:
# - Compatible with macOS default Bash 3.2
# - Safe under `set -euo pipefail` (avoid unbound var errors)
#
# Environment:
# - RALPH_UI:   auto|gum|plain  (default: auto)
# - GUM_FORCE:  1 to force gum even if not a TTY (not recommended for CI logs)
# - NO_COLOR:   disable ANSI colors (standard)
# - RALPH_ASCII:1 to use ASCII separators instead of box-drawing chars
# =============================================================================

# shellcheck disable=SC2034  # many vars are used indirectly by callers

ui__has_cmd() { command -v "$1" >/dev/null 2>&1; }
ui__is_tty_fd() { local fd="$1"; [[ -t "$fd" ]]; }
ui__lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
ui__trim_ws() {
  # Trim leading/trailing whitespace (Bash 3.2 safe).
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

ui__ascii() { [[ "${RALPH_ASCII-}" == "1" ]]; }

ui__rule_char() {
  if ui__ascii; then
    printf '%s' '-'
  else
    printf '%s' '─'
  fi
}

ui__pipe_char() {
  if ui__ascii; then
    printf '%s' '|'
  else
    printf '%s' '│'
  fi
}

ui__term_cols() {
  local cols="${COLUMNS-}"
  if ui__has_cmd tput; then
    cols="$(tput cols 2>/dev/null || true)"
  fi
  cols="${cols:-80}"
  printf '%s' "$cols"
}

ui__use_gum_fd() {
  local fd="$1"
  local mode="${RALPH_UI-auto}"

  ui__has_cmd gum || return 1

  case "$mode" in
    gum) return 0 ;;
    plain|off|no|0) return 1 ;;
    auto|"")
      if ui__is_tty_fd "$fd" || [[ "${GUM_FORCE-}" == "1" ]]; then
        return 0
      fi
      return 1
      ;;
    *)
      # Unknown value: be conservative
      return 1
      ;;
  esac
}

ui__use_color_fd() {
  local fd="$1"
  [[ -n "${NO_COLOR-}" ]] && return 1
  [[ "${TERM-}" == "dumb" ]] && return 1
  ui__is_tty_fd "$fd" || return 1
  return 0
}

ui__ansi() {
  # Print ANSI code if colors enabled for fd; else empty.
  local fd="$1"
  local code="$2"
  if ui__use_color_fd "$fd"; then
    printf '\033[%sm' "$code"
  else
    printf '%s' ''
  fi
}

ui__ansi_reset() { ui__ansi "$1" '0'; }

ui_hr_fd() {
  local fd="$1"
  local cols; cols="$(ui__term_cols)"
  local ch; ch="$(ui__rule_char)"
  # tr is fine here; used only for separator generation.
  printf '%*s\n' "$cols" '' | tr ' ' "$ch" >&"$fd"
}

ui_blank_fd() { local fd="$1"; printf '\n' >&"$fd"; }

ui_title_fd() {
  local fd="$1"
  local text="$2"
  if ui__use_gum_fd "$fd"; then
    gum style \
      --border double \
      --padding "1 2" \
      --margin "1 0" \
      --align center \
      --bold \
      --foreground 212 \
      "$text" >&"$fd"
  else
    ui_blank_fd "$fd"
    ui_hr_fd "$fd"
    printf '%s\n' "$text" >&"$fd"
    ui_hr_fd "$fd"
    ui_blank_fd "$fd"
  fi
}

ui_section_fd() {
  local fd="$1"
  local text="$2"
  if ui__use_gum_fd "$fd"; then
    gum style --bold --foreground 99 --margin "1 0" "$text" >&"$fd"
  else
    ui_blank_fd "$fd"
    printf '== %s ==\n' "$text" >&"$fd"
  fi
}

ui_subsection_fd() {
  local fd="$1"
  local text="$2"
  if ui__use_gum_fd "$fd"; then
    gum style --bold --foreground 69 "$text" >&"$fd"
  else
    printf -- '-- %s --\n' "$text" >&"$fd"
  fi
}

ui_box_fd() {
  # Read stdin, print as boxed/indented block to fd.
  local fd="$1"
  local content
  content="$(cat)"

  if [[ -z "$content" ]]; then
    return 0
  fi

  if ui__use_gum_fd "$fd"; then
    gum style --border normal --padding "0 1" --margin "0 0" "$content" >&"$fd"
  else
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
      printf '  %s\n' "$line" >&"$fd"
    done <<< "$content"
  fi
}

ui_kv_fd() {
  local fd="$1"
  local key="$2"
  local value="$3"
  printf '%-14s %s\n' "${key}:" "$value" >&"$fd"
}

ui_info_fd() {
  local fd="$1"
  local text="$2"
  local dim; dim="$(ui__ansi "$fd" '2')"
  local reset; reset="$(ui__ansi_reset "$fd")"
  printf '%s%s%s\n' "$dim" "$text" "$reset" >&"$fd"
}

ui_ok_fd() {
  local fd="$1"
  local text="$2"
  if ui__use_gum_fd "$fd"; then
    gum style --foreground 10 --bold "OK: $text" >&"$fd"
  else
    local green; green="$(ui__ansi "$fd" '32')"
    local reset; reset="$(ui__ansi_reset "$fd")"
    printf '%sOK:%s %s\n' "$green" "$reset" "$text" >&"$fd"
  fi
}

ui_warn_fd() {
  local fd="$1"
  local text="$2"
  if ui__use_gum_fd "$fd"; then
    gum style --foreground 11 --bold "WARN: $text" >&"$fd"
  else
    local yellow; yellow="$(ui__ansi "$fd" '33')"
    local reset; reset="$(ui__ansi_reset "$fd")"
    printf '%sWARN:%s %s\n' "$yellow" "$reset" "$text" >&"$fd"
  fi
}

ui_err_fd() {
  local fd="$1"
  local text="$2"
  if ui__use_gum_fd "$fd"; then
    gum style --foreground 9 --bold "ERROR: $text" >&"$fd"
  else
    local red; red="$(ui__ansi "$fd" '31')"
    local reset; reset="$(ui__ansi_reset "$fd")"
    printf '%sERROR:%s %s\n' "$red" "$reset" "$text" >&"$fd"
  fi
}

ui_channel_header_fd() {
  # A demarcated header for a "channel" block (AI / USER / SYS / GIT / etc).
  # Usage: ui_channel_header_fd 2 "AI" "Agent output"
  local fd="$1"
  local channel="$2"
  local title="$3"

  local label="$channel"
  [[ -n "$title" ]] && label="$channel · $title"

  if ui__use_gum_fd "$fd"; then
    local fg=255
    local bg=240
    case "$channel" in
      AI) bg=99 ;;
      USER) bg=31 ;;
      PROMPT) bg=31 ;;
      THINK) bg=99 ;;
      SYS) bg=240 ;;
      TOOL) bg=214 ;;
      GIT) bg=25 ;;
      GUARD) bg=130 ;;
    esac
    gum style --bold --foreground "$fg" --background "$bg" --padding "0 1" "$label" >&"$fd"
  else
    ui_blank_fd "$fd"
    ui_hr_fd "$fd"
    printf '%s\n' "$label" >&"$fd"
    ui_hr_fd "$fd"
  fi
}

ui_channel_footer_fd() {
  local fd="$1"
  local channel="$2"
  local title="${3-}"

  local label="$channel"
  [[ -n "$title" ]] && label="$channel · $title"

  if ui__use_gum_fd "$fd"; then
    gum style --faint "end: $label" >&"$fd"
  else
    printf 'end: %s\n' "$label" >&"$fd"
  fi
}

ui_stream_prefix_fd() {
  # Stream stdin to fd with a colored prefix, line-by-line.
  # Usage: cmd | ui_stream_prefix_fd 2 "AI"
  local fd="$1"
  local tag="$2"
  local sep; sep="$(ui__pipe_char)"

  local color_tag=""
  local reset=""
  if ui__use_color_fd "$fd"; then
    reset="$(ui__ansi_reset "$fd")"
    case "$tag" in
      AI) color_tag="$(ui__ansi "$fd" '35;1')" ;;      # bold magenta
      THINK) color_tag="$(ui__ansi "$fd" '35;2')" ;;   # dim magenta (if supported)
      USER) color_tag="$(ui__ansi "$fd" '36;1')" ;;    # bold cyan
      PROMPT) color_tag="$(ui__ansi "$fd" '36;1')" ;;  # bold cyan
      SYS) color_tag="$(ui__ansi "$fd" '90;1')" ;;     # bold gray
      TOOL) color_tag="$(ui__ansi "$fd" '38;5;214;1')" ;; # bold orange
      GIT) color_tag="$(ui__ansi "$fd" '34;1')" ;;     # bold blue
      GUARD) color_tag="$(ui__ansi "$fd" '33;1')" ;;   # bold yellow
      *) color_tag="$(ui__ansi "$fd" '1')" ;;
    esac
  fi

  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ -n "$color_tag" ]]; then
      printf '%s%s%s %s %s\n' "$color_tag" "$tag" "$reset" "$sep" "$line" >&"$fd"
    else
      printf '%s %s %s\n' "$tag" "$sep" "$line" >&"$fd"
    fi
  done
}

ui_print_prefixed_fd() {
  # Print one line with a tag prefix.
  # Usage: ui_print_prefixed_fd 2 "AI" "hello"
  local fd="$1"
  local tag="$2"
  local line="${3-}"
  local sep; sep="$(ui__pipe_char)"

  if [[ -z "$line" ]]; then
    printf '\n' >&"$fd"
    return 0
  fi

  local color_tag=""
  local reset=""
  if ui__use_color_fd "$fd"; then
    reset="$(ui__ansi_reset "$fd")"
    case "$tag" in
      AI) color_tag="$(ui__ansi "$fd" '35;1')" ;;      # bold magenta
      THINK) color_tag="$(ui__ansi "$fd" '35;2')" ;;   # dim magenta (if supported)
      USER) color_tag="$(ui__ansi "$fd" '36;1')" ;;    # bold cyan
      PROMPT) color_tag="$(ui__ansi "$fd" '36;1')" ;;  # bold cyan
      SYS) color_tag="$(ui__ansi "$fd" '90;1')" ;;     # bold gray
      TOOL) color_tag="$(ui__ansi "$fd" '38;5;214;1')" ;; # bold orange
      GIT) color_tag="$(ui__ansi "$fd" '34;1')" ;;     # bold blue
      GUARD) color_tag="$(ui__ansi "$fd" '33;1')" ;;   # bold yellow
      *) color_tag="$(ui__ansi "$fd" '1')" ;;
    esac
  fi

  if [[ -n "$color_tag" ]]; then
    printf '%s%s%s %s %s\n' "$color_tag" "$tag" "$reset" "$sep" "$line" >&"$fd"
  else
    printf '%s %s %s\n' "$tag" "$sep" "$line" >&"$fd"
  fi
}

ui__md_style_line_to() {
  # Very lightweight “markdown-ish” styling via ANSI (no external deps).
  # Writes the styled line into a variable (avoids subshells per line).
  #
  # Args: fd line in_code outvar
  local fd="$1"
  local line="$2"
  local in_code="${3:-0}"
  local outvar="$4"

  local out="$line"

  if ui__use_color_fd "$fd"; then
    local reset; reset="$(ui__ansi_reset "$fd")"
    local bold; bold="$(ui__ansi "$fd" '1')"
    local dim; dim="$(ui__ansi "$fd" '2')"
    local h; h="$(ui__ansi "$fd" '38;5;212;1')"         # pink-ish bold
    local code; code="$(ui__ansi "$fd" '38;5;252')"     # light fg
    local codebg; codebg="$(ui__ansi "$fd" '48;5;234')" # dark bg
    local ok; ok="$(ui__ansi "$fd" '32;1')"

    if [[ "$line" == \`\`\`* ]] || (( in_code == 1 )); then
      out="${codebg}${code}${line}${reset}"
    elif [[ "$line" =~ ^#{1,6}[[:space:]] ]]; then
      out="${h}${line}${reset}"
    elif [[ "$line" == *"<promise>COMPLETE</promise>"* ]]; then
      out="${ok}${line}${reset}"
    elif [[ "$line" =~ ^(-|\*|[0-9]+\.)[[:space:]] ]]; then
      out="${bold}${line}${reset}"
    elif [[ "$line" =~ ^[-_=]{3,}$ ]]; then
      out="${dim}${line}${reset}"
    fi
  fi

  printf -v "$outvar" '%s' "$out"
}

ui_tee_ai_pretty_err() {
  # Like ui_tee_prefix_err, but makes the AI output easier to read by:
  # - highlighting headings/code fences
  # - keeping a consistent AI prefix
  # Still writes the raw stream to stdout for capturing.
  local in_code=0
  local line
  local styled=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    printf '%s\n' "$line"
    if [[ "$line" == \`\`\`* ]]; then
      # Toggle before rendering line (so the fence itself is styled consistently)
      if (( in_code == 1 )); then in_code=0; else in_code=1; fi
      ui__md_style_line_to 2 "$line" "$in_code" styled
      ui_print_prefixed_fd 2 "AI" "$styled"
      continue
    fi
    ui__md_style_line_to 2 "$line" "$in_code" styled
    ui_print_prefixed_fd 2 "AI" "$styled"
  done
}

ui_ai_pretty_stream_fd() {
  # Pretty-print stdin as AI output to the given fd (no stdout duplication).
  # Usage: cat file | ui_ai_pretty_stream_fd 2 AI
  local fd="$1"
  local tag="${2:-AI}"
  local in_code=0
  local line
  local styled=""

  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == \`\`\`* ]]; then
      if (( in_code == 1 )); then in_code=0; else in_code=1; fi
    fi
    ui__md_style_line_to "$fd" "$line" "$in_code" styled
    ui_print_prefixed_fd "$fd" "$tag" "$styled"
  done
}

ui_codex_pretty_stream_fd() {
  # Improve Codex transcript readability:
  # - Tag lines by role: SYS / PROMPT / THINK / AI / TOOL
  # - Hide the echoed user prompt by default (it’s the prompt file content).
  #   Importantly: only suppress lines that match the actual PROMPT_FILE content.
  # - Apply markdown-ish styling to assistant lines
  #
  # Usage:
  #   codex ... 2>&1 | ui_codex_pretty_stream_fd 2 "$PROMPT_FILE"
  local fd="$1"
  local prompt_file="${2-}"

  local show_prompt="${RALPH_AI_SHOW_PROMPT-}"
  local progress_every="${RALPH_AI_PROMPT_PROGRESS_EVERY-50}"
  local role="SYS"
  local in_code=0
  local hidden_prompt_lines=0
  local styled=""
  local prompt_header_printed=""
  local prompt_summary_printed=""
  local prompt_hide_active=""

  # If we can't load the prompt file, don't suppress (avoid hiding real output).
  local -a prompt_lines=()
  if [[ -n "$prompt_file" ]] && [[ -f "$prompt_file" ]]; then
    local pline
    while IFS= read -r pline || [[ -n "$pline" ]]; do
      pline="${pline%$'\r'}"
      prompt_lines+=("$pline")
    done < "$prompt_file"
  fi
  local prompt_i=0

  if [[ -z "$show_prompt" ]] && (( ${#prompt_lines[@]} > 0 )); then
    prompt_hide_active="1"
  fi
  local src="prompt"
  [[ -n "$prompt_file" ]] && src="$prompt_file"

  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Role markers in codex transcript are usually bare lines; trim whitespace
    # and accept common suffixes like ":" to be more robust.
    local marker="$line"
    marker="${marker%$'\r'}"
    marker="$(ui__trim_ws "$marker")"
    marker="${marker%:}"
    marker="$(ui__lower "$marker")"

    case "$marker" in
      user)
        role="PROMPT"
        hidden_prompt_lines=0
        in_code=0
        prompt_i=0
        prompt_header_printed=""
        prompt_summary_printed=""
        # Don't print the role marker itself (noise)
        continue
        ;;
      assistant|codex|final)
        # If we were hiding the prompt, print a one-line summary before AI begins.
        if [[ "$role" == "PROMPT" ]] && [[ -n "$prompt_hide_active" ]] && [[ -z "$prompt_summary_printed" ]] && (( hidden_prompt_lines > 0 )); then
          ui_print_prefixed_fd "$fd" "PROMPT" "[prompt hidden: $src · ${hidden_prompt_lines} lines suppressed]"
          prompt_summary_printed="1"
        fi
        prompt_hide_active=""
        role="AI"
        in_code=0
        continue
        ;;
      thinking|analysis)
        if [[ "$role" == "PROMPT" ]] && [[ -n "$prompt_hide_active" ]] && [[ -z "$prompt_summary_printed" ]] && (( hidden_prompt_lines > 0 )); then
          ui_print_prefixed_fd "$fd" "PROMPT" "[prompt hidden: $src · ${hidden_prompt_lines} lines suppressed]"
          prompt_summary_printed="1"
        fi
        prompt_hide_active=""
        role="THINK"
        in_code=0
        continue
        ;;
      tool)
        role="TOOL"
        in_code=0
        continue
        ;;
      exec)
        role="TOOL"
        in_code=0
        continue
        ;;
      system)
        role="SYS"
        in_code=0
        continue
        ;;
    esac

    if [[ "$role" == "PROMPT" ]] && [[ -n "$prompt_hide_active" ]]; then
      # Only suppress lines that match the prompt file content.
      local oline="$line"
      oline="${oline%$'\r'}"

      # If we already consumed the entire prompt file, stop hiding (the prompt is over).
      if (( prompt_i >= ${#prompt_lines[@]} )); then
        if [[ -z "$prompt_summary_printed" ]] && (( hidden_prompt_lines > 0 )); then
          ui_print_prefixed_fd "$fd" "PROMPT" "[prompt hidden: $src · ${hidden_prompt_lines} lines suppressed]"
          prompt_summary_printed="1"
        fi
        prompt_hide_active=""
        role="SYS"
        # fall through to print this line with the new role
      elif [[ "$oline" == "${prompt_lines[$prompt_i]}" ]]; then
        prompt_i=$((prompt_i + 1))
        hidden_prompt_lines=$((hidden_prompt_lines + 1))

        if [[ -z "$prompt_header_printed" ]]; then
          ui_print_prefixed_fd "$fd" "PROMPT" "[prompt hidden: $src]"
          prompt_header_printed="1"
        fi

        # Periodically emit progress so long prompts don't look like a hang.
        if [[ "$progress_every" =~ ^[0-9]+$ ]] && (( progress_every > 0 )) && (( hidden_prompt_lines % progress_every == 0 )); then
          ui_print_prefixed_fd "$fd" "PROMPT" "[prompt hidden: $src · ${hidden_prompt_lines} lines suppressed]"
        fi
        continue
      else
        # Mismatch: stop suppressing to avoid hiding real output.
        if [[ -z "$prompt_summary_printed" ]] && (( hidden_prompt_lines > 0 )); then
          ui_print_prefixed_fd "$fd" "PROMPT" "[prompt hidden: $src · ${hidden_prompt_lines} lines suppressed]"
          prompt_summary_printed="1"
        fi
        ui_print_prefixed_fd "$fd" "SYS" "[prompt hiding disabled: output diverged from $src]"
        prompt_hide_active=""
        role="SYS"
        # fall through to print this line with the new role
      fi
    fi

    if [[ "$role" == "AI" || "$role" == "THINK" ]]; then
      if [[ "$line" == \`\`\`* ]]; then
        if (( in_code == 1 )); then in_code=0; else in_code=1; fi
        ui__md_style_line_to "$fd" "$line" "$in_code" styled
        ui_print_prefixed_fd "$fd" "$role" "$styled"
      else
        ui__md_style_line_to "$fd" "$line" "$in_code" styled
        ui_print_prefixed_fd "$fd" "$role" "$styled"
      fi
      continue
    fi

    # TOOL / SYS / USER (when showing prompt)
    ui_print_prefixed_fd "$fd" "$role" "$line"
  done
}

ui_tee_prefix_err() {
  # Like `tee`, but:
  # - writes the ORIGINAL stream to stdout (for capture)
  # - writes a prefixed stream to stderr (for human viewing)
  # Usage: OUTPUT="$(cmd 2>&1 | ui_tee_prefix_err AI)" ; # OUTPUT contains raw
  local tag="$1"

  local sep; sep="$(ui__pipe_char)"

  local color_tag=""
  local reset=""
  if ui__use_color_fd 2; then
    reset="$(ui__ansi_reset 2)"
    case "$tag" in
      AI) color_tag="$(ui__ansi 2 '35;1')" ;;      # bold magenta
      THINK) color_tag="$(ui__ansi 2 '35;2')" ;;   # dim magenta (if supported)
      USER) color_tag="$(ui__ansi 2 '36;1')" ;;    # bold cyan
      PROMPT) color_tag="$(ui__ansi 2 '36;1')" ;;  # bold cyan
      SYS) color_tag="$(ui__ansi 2 '90;1')" ;;     # bold gray
      TOOL) color_tag="$(ui__ansi 2 '38;5;214;1')" ;; # bold orange
      GIT) color_tag="$(ui__ansi 2 '34;1')" ;;     # bold blue
      GUARD) color_tag="$(ui__ansi 2 '33;1')" ;;   # bold yellow
      *) color_tag="$(ui__ansi 2 '1')" ;;
    esac
  fi

  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    printf '%s\n' "$line"
    if [[ -n "$color_tag" ]]; then
      printf '%s%s%s %s %s\n' "$color_tag" "$tag" "$reset" "$sep" "$line" >&2
    else
      printf '%s %s %s\n' "$tag" "$sep" "$line" >&2
    fi
  done
}

ui_can_prompt() {
  # True if we can safely prompt the user (stdin is a TTY).
  [[ -t 0 ]]
}

ui_choose_fd() {
  # Choose among options. If gum is available and stdin is a TTY, use gum.
  # Otherwise, fall back to a simple numbered prompt (TTY only) or return the
  # provided default (non-TTY).
  #
  # Usage:
  #   choice="$(ui_choose_fd 1 "Header" "Default" "Opt1" "Opt2")"
  local fd="$1"
  local header="$2"
  local default="$3"
  shift 3

  if ui__use_gum_fd "$fd" && ui_can_prompt; then
    local out=""
    set +e
    out="$(gum choose --header "$header" "$@")"
    local rc="$?"
    set -e
    if (( rc == 0 )) && [[ -n "$out" ]]; then
      printf '%s\n' "$out"
      return 0
    fi
  fi

  # Non-interactive: pick default (prevents hangs in CI / piped runs).
  if ! ui_can_prompt; then
    printf '%s\n' "$default"
    return 0
  fi

  # Plain interactive fallback (TTY): numbered menu + single-letter shortcuts.
  local opts=("${@+"$@"}")
  {
    printf '%s\n' "$header"
    local idx=1
    local opt
    for opt in "${opts[@]+"${opts[@]}"}"; do
      printf '  [%d] %s\n' "$idx" "$opt"
      idx=$((idx + 1))
    done
    printf '\n'
    printf 'Choose [default: %s]: ' "$default"
  } >&"$fd"

  local answer=""
  IFS= read -r answer || true
  answer="${answer#"${answer%%[![:space:]]*}"}"
  answer="${answer%"${answer##*[![:space:]]}"}"

  if [[ -z "$answer" ]]; then
    printf '%s\n' "$default"
    return 0
  fi

  # Numeric selection
  if [[ "$answer" =~ ^[0-9]+$ ]]; then
    local n="$answer"
    if (( n >= 1 && n <= ${#opts[@]} )); then
      printf '%s\n' "${opts[$((n - 1))]}"
      return 0
    fi
    printf '%s\n' "$default"
    return 0
  fi

  # Single-letter shortcut: pick the first option starting with that letter.
  if [[ "$answer" =~ ^[[:alpha:]]$ ]]; then
    local lower; lower="$(ui__lower "$answer")"
    local match=""
    local mcount=0
    local o
    for o in "${opts[@]+"${opts[@]}"}"; do
      local olower; olower="$(ui__lower "$o")"
      if [[ "${olower:0:1}" == "$lower" ]]; then
        match="$o"
        mcount=$((mcount + 1))
      fi
    done
    if (( mcount == 1 )) && [[ -n "$match" ]]; then
      printf '%s\n' "$match"
      return 0
    fi
  fi

  # Exact / case-insensitive match fallback
  local o
  local alower; alower="$(ui__lower "$answer")"
  for o in "${opts[@]+"${opts[@]}"}"; do
    if [[ "$(ui__lower "$o")" == "$alower" ]]; then
      printf '%s\n' "$o"
      return 0
    fi
  done

  printf '%s\n' "$default"
}

ui_confirm_fd() {
  # Confirm yes/no. If gum is available and stdin is a TTY, use gum confirm.
  # Otherwise, fall back to a simple y/N prompt (TTY only) or return default
  # (non-TTY).
  local fd="$1"
  local prompt="$2"
  local default_yes="${3:-0}"

  if ui__use_gum_fd "$fd" && ui_can_prompt; then
    if gum confirm "$prompt" >&"$fd"; then
      return 0
    fi
    return 1
  fi

  if ! ui_can_prompt; then
    return "$default_yes"
  fi

  local suffix="[y/N]"
  if (( default_yes == 0 )); then
    suffix="[Y/n]"
  fi

  printf '%s %s ' "$prompt" "$suffix" >&"$fd"
  local answer=""
  IFS= read -r answer || true
  answer="${answer#"${answer%%[![:space:]]*}"}"
  answer="${answer%"${answer##*[![:space:]]}"}"

  if [[ -z "$answer" ]]; then
    return "$default_yes"
  fi

  case "$(ui__lower "$answer")" in
    y|yes) return 0 ;;
    n|no) return 1 ;;
  esac

  return "$default_yes"
}

ui_mode() {
  if ui__use_gum_fd 1 || ui__use_gum_fd 2; then
    printf '%s' 'gum'
  else
    printf '%s' 'plain'
  fi
}

# Convenience wrappers (stdout)
ui_title() { ui_title_fd 1 "$1"; }
ui_section() { ui_section_fd 1 "$1"; }
ui_subsection() { ui_subsection_fd 1 "$1"; }
ui_box() { ui_box_fd 1; }
ui_kv() { ui_kv_fd 1 "$1" "$2"; }
ui_info() { ui_info_fd 1 "$1"; }
ui_ok() { ui_ok_fd 1 "$1"; }
ui_warn() { ui_warn_fd 1 "$1"; }
ui_err() { ui_err_fd 1 "$1"; }
ui_channel_header() { ui_channel_header_fd 1 "$1" "${2-}"; }
ui_channel_footer() { ui_channel_footer_fd 1 "$1" "${2-}"; }

# Convenience wrappers (stderr)
ui_title_err() { ui_title_fd 2 "$1"; }
ui_section_err() { ui_section_fd 2 "$1"; }
ui_subsection_err() { ui_subsection_fd 2 "$1"; }
ui_box_err() { ui_box_fd 2; }
ui_info_err() { ui_info_fd 2 "$1"; }
ui_ok_err() { ui_ok_fd 2 "$1"; }
ui_warn_err() { ui_warn_fd 2 "$1"; }
ui_err_err() { ui_err_fd 2 "$1"; }
ui_channel_header_err() { ui_channel_header_fd 2 "$1" "${2-}"; }
ui_channel_footer_err() { ui_channel_footer_fd 2 "$1" "${2-}"; }

