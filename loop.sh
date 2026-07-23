#!/bin/bash
# Ralph Wiggum loop, adapted for Claude Code from
# https://github.com/gwincr11/ralph-wiggum-tutorial (shown in class).
# Changes from the original: runs `claude -p` instead of Copilot CLI,
# and only pushes if a git remote is configured.
#
# Usage: ./loop.sh [options]
# Options:
#   -m, --mode <plan|build>      Mode (default: build)
#   -n, --max <number>           Max iterations, 0 for unlimited (default: 0)
#   -s, --stop <string>          Completion promise - stop when this string appears in output
#   -p, --prompt <file>          Custom prompt file (overrides mode default)
#   -h, --help                   Show this help message
#
# Examples:
#   ./loop.sh                                    # Build mode, unlimited
#   ./loop.sh -m plan -n 3                       # Plan mode, max 3 iterations
#   ./loop.sh -n 10 -s "DONE"                    # Build mode, max 10 or stop on "DONE"
#   ./loop.sh -p custom_prompt.md -n 3           # Custom prompt file

# Defaults
MODE="build"
MAX_ITERATIONS=0
COMPLETION_PROMISE="<promise>DONE</promise>"
PROMPT_FILE=""

# Parse named arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -m|--mode)
            MODE="$2"
            shift 2
            ;;
        -n|--max)
            MAX_ITERATIONS="$2"
            shift 2
            ;;
        -s|--stop)
            COMPLETION_PROMISE="$2"
            shift 2
            ;;
        -p|--prompt)
            PROMPT_FILE="$2"
            shift 2
            ;;
        -h|--help)
            head -n 19 "$0" | tail -n 14
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage"
            exit 1
            ;;
    esac
done

# Set default prompt file based on mode if not specified
if [ -z "$PROMPT_FILE" ]; then
    if [ "$MODE" = "plan" ]; then
        PROMPT_FILE="PROMPT_plan.md"
    else
        PROMPT_FILE="PROMPT_build.md"
    fi
fi

ITERATION=0
CURRENT_BRANCH=$(git branch --show-current)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Mode:   $MODE"
echo "Prompt: $PROMPT_FILE"
echo "Branch: $CURRENT_BRANCH"
[ -n "$COMPLETION_PROMISE" ] && echo "Stop:   '$COMPLETION_PROMISE'"
[ "$MAX_ITERATIONS" -gt 0 ] && echo "Max:    $MAX_ITERATIONS iterations"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Verify prompt file exists
if [ ! -f "$PROMPT_FILE" ]; then
    echo "Error: $PROMPT_FILE not found"
    exit 1
fi

while true; do
    if [ "$MAX_ITERATIONS" -gt 0 ] && [ "$ITERATION" -ge "$MAX_ITERATIONS" ]; then
        echo "Reached max iterations: $MAX_ITERATIONS"
        break
    fi

    # Run one Ralph iteration with the selected prompt.
    PROMPT_CONTENT=$(cat "$PROMPT_FILE")

    # Capture output while still displaying it
    OUTPUT=$(claude -p "$PROMPT_CONTENT" --dangerously-skip-permissions 2>&1 | tee /dev/stderr)

    # Push changes after each iteration (skip if no remote configured yet)
    if git remote get-url origin &>/dev/null; then
        git push origin "$CURRENT_BRANCH" || {
            echo "Failed to push. Creating remote branch..."
            git push -u origin "$CURRENT_BRANCH"
        }
    fi

    # Check for completion promise in output
    if [ -n "$COMPLETION_PROMISE" ] && echo "$OUTPUT" | grep -qF "$COMPLETION_PROMISE"; then
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✓ Completion promise found: '$COMPLETION_PROMISE'"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        break
    fi

    ITERATION=$((ITERATION + 1))
    echo -e "\n\n======================== LOOP $ITERATION ========================\n"
done
