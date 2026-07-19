# Interactive launcher identity for Codex Ultracode.
# Source this file from .zshrc after the external `codex` command is on PATH.

typeset -g _codex_ultracode_binary="${commands[codex]:-}"
typeset -g _codex_ultracode_identity_hook="${${(%):-%x}:A:h:h}/hooks/session_start.py"

if [[ -n "$_codex_ultracode_binary" ]]; then
    function codex {
        if (( $# == 0 )); then
            local message
            message="$(command python3 "$_codex_ultracode_identity_hook" --identity-message 2>/dev/null)" || \
                message="Codex with Ultracode — specialist agent workflows ready (Conductor not detected)."
            if [[ -z "$message" ]]; then
                message="Codex with Ultracode — specialist agent workflows ready (Conductor not detected)."
            fi
            if [[ -t 1 ]]; then
                print -P -- "%F{cyan}%B${message}%b%f"
            else
                print -r -- "$message"
            fi
            CODEX_ULTRACODE_STARTUP_SHOWN=1 "$_codex_ultracode_binary"
        else
            "$_codex_ultracode_binary" "$@"
        fi
    }
fi
