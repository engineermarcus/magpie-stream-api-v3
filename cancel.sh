cd ~/magpie-stream-api-v3
gh run list --workflow=stream-worker.yml --limit 50 --json databaseId,status | python3 -c "
import sys, json
runs = json.load(sys.stdin)
in_progress = [str(r['databaseId']) for r in runs if r['status'] == 'in_progress']
print('\n'.join(in_progress))
" | xargs -I{} gh run cancel {}
