cd ~/magpie-stream-api-v3

# cancel all in progress
gh run list --workflow=stream-worker.yml --limit 100 --json databaseId,status | python3 -c "
import sys, json
runs = json.load(sys.stdin)
ids = [str(r['databaseId']) for r in runs if r['status'] == 'in_progress']
print('\n'.join(ids))
" | xargs -I{} gh run cancel {}

sleep 15

# delete all
gh run list --workflow=stream-worker.yml --limit 100 --json databaseId | python3 -c "
import sys, json
ids = [str(r['databaseId']) for r in json.load(sys.stdin)]
print('\n'.join(ids))
" | xargs -I{} gh run delete {}
