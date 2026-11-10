#!/usr/bin/env bash
# delete-closed-issues.sh
#
# Permanently DELETE every CLOSED issue in one or more GitHub repos, ONE AT A
# TIME, with a per-issue confirmation prompt. Uses the GitHub GraphQL
# `deleteIssue` mutation (the REST API cannot delete issues).
#
# *** DELETION IS PERMANENT AND IRREVERSIBLE. ***
# The issue body AND every comment from every contributor are destroyed forever.
#
# Requirements:
#   - curl, jq
#   - A token whose account has ADMIN permission on each target repo, with
#     `repo` scope (classic PAT) — or `public_repo` for public repos. The
#     built-in GITHUB_TOKEN from Actions CANNOT delete issues.
#     By default the script uses `gh auth token`; override with GH_TOKEN.
#   - macOS/Linux bash. (No mapfile; uses an FD + temp file so prompts work.)
#
# Usage:
#   ./delete-closed-issues.sh owner/repo                  # interactive (default)
#   ./delete-closed-issues.sh owner/repo --list           # just list, delete nothing
#   ./delete-closed-issues.sh owner/repo1 owner/repo2     # multiple repos
#   GH_TOKEN=ghp_xxx ./delete-closed-issues.sh owner/repo
#   ./delete-closed-issues.sh owner/repo --yes            # NO PROMPT — deletes all (DANGER)
#
# To find a repo's owner/name:  git -C <repo-dir> remote get-url origin
#   (e.g. git@github.com:acme/lm.git  ->  acme/lm)

set -euo pipefail

MODE="interactive"      # interactive | list | yes
REPOS=()

for arg in "$@"; do
  case "$arg" in
    --list) MODE="list" ;;
    --yes)  MODE="yes" ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *) REPOS+=("$arg") ;;
  esac
done

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "Usage: $0 owner/repo [owner/repo2 ...] [--list|--yes]" >&2
  exit 1
fi

for r in "${REPOS[@]}"; do
  if [[ "$r" != *"/"* ]]; then
    echo "Bad repo spec '$r' (expected owner/name)." >&2
    exit 1
  fi
done

TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "$TOKEN" ]] && command -v gh >/dev/null 2>&1; then
  TOKEN="$(gh auth token 2>/dev/null || true)"
fi
if [[ -z "$TOKEN" ]]; then
  echo "No GitHub token. Run 'gh auth login' or set GH_TOKEN." >&2
  exit 1
fi

GQL="https://api.github.com/graphql"

gql() {  # $1 = JSON body (from jq)
  curl -sS -X POST "$GQL" \
    -H "Authorization: bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$1"
}

gql_errors() { echo "$1" | jq -e '.errors' >/dev/null 2>&1; }

# Stream closed issues for one repo into a temp file: node_id \t number \t title \t url
fetch_closed() {
  local owner="$1" name="$2" out="$3" cursor="null"
  : > "$out"
  while true; do
    local body resp
    body=$(jq -n --arg o "$owner" --arg n "$name" --argjson c "$cursor" '{
      query: "query($o:String!,$n:String!,$c:String){repository(owner:$o,name:$n){issues(first:100,after:$c,states:[CLOSED],orderBy:{field:CREATED_AT,direction:ASC}){pageInfo{endCursor hasNextPage}nodes{id number title url}}}}",
      variables: {o:$o,n:$n,c:$c}
    }')
    resp=$(gql "$body")
    if gql_errors "$resp"; then
      echo "GraphQL error listing $owner/$name:" >&2
      echo "$resp" | jq '.errors' >&2
      return 1
    fi
    echo "$resp" | jq -r '.data.repository.issues.nodes[] | [.id,.number,.title,.url] | @tsv' >> "$out"
    [[ "$(echo "$resp" | jq -r '.data.repository.issues.pageInfo.hasNextPage')" == "true" ]] || break
    cursor=$(echo "$resp" | jq -r '.data.repository.issues.pageInfo.endCursor')
  done
}

delete_issue() {  # $1 = node_id
  local body resp
  body=$(jq -n --arg id "$1" '{
    query: "mutation($id:ID!){deleteIssue(input:{issueId:$id}){clientMutationId}}",
    variables: {id:$id}
  }')
  resp=$(gql "$body")
  if gql_errors "$resp"; then
    echo "  FAILED:" >&2
    echo "$resp" | jq -r '.errors[].message' >&2
    return 1
  fi
  echo "  deleted."
}

TMP="$(mktemp -t delissues.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

for spec in "${REPOS[@]}"; do
  owner="${spec%%/*}"
  name="${spec#*/}"
  echo "================================================================"
  echo "Repo: $owner/$name  (mode: $MODE)"
  echo "Fetching CLOSED issues..."
  if ! fetch_closed "$owner" "$name" "$TMP"; then continue; fi
  count=$(wc -l < "$TMP" | tr -d ' ')
  echo "Found $count closed issue(s)."
  [[ "$count" -eq 0 ]] && { echo; continue; }

  if [[ "$MODE" == "list" ]]; then
    while IFS=$'\t' read -r id num title url; do
      printf '#%-6s %s\n   %s\n' "$num" "$title" "$url"
    done < "$TMP"
    echo; continue
  fi

  idx=0
  exec 3< "$TMP"
  while IFS=$'\t' read -r id num title url <&3; do
    idx=$((idx+1))
    echo "[$idx/$count] #$num — $title"
    echo "   $url"
    if [[ "$MODE" == "yes" ]]; then
      ans="y"
    else
      read -r -p "   Permanently delete? [y/N/q] " ans || ans="n"
    fi
    case "$ans" in
      y|Y) delete_issue "$id" || true ;;
      q|Q) echo "Quitting." >&2; exit 130 ;;
      *)   echo "   skipped." ;;
    esac
    echo
  done
  exec 3<&-
done

echo "Done."