import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy import rewrite_body, rewrite_messages

IMG = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
ask = {'role': 'user', 'content': 'read it'}
call = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {}}]}
reply = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'done'}]}


def roles(msgs):
    return [m['role'] for m in msgs]


# 1. image in tool_result -> moved to synthetic turn, alternation preserved
tr = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': [IMG]},
                                  {'type': 'text', 'text': '<system-reminder>x</system-reminder>'}]}
msgs = [ask, call, tr, reply]
out, moved = rewrite_messages(msgs)
assert moved == 1, moved
assert roles(out) == ['user', 'assistant', 'user', 'assistant', 'user', 'assistant'], roles(out)
assert out[2]['content'][0]['content'][-1]['type'] == 'text'           # placeholder left in tool_result
assert all(b.get('type') != 'image' for b in out[2]['content'][0]['content'])
assert [b['type'] for b in out[4]['content']] == ['text', 'text', 'image', 'text']  # intro, label, image, reminder
assert msgs[2]['content'][0]['content'] == [IMG], 'input mutated'

# 2. image next to tool_result -> moved too
tr2 = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}, IMG]}
out, moved = rewrite_messages([ask, call, tr2])
assert moved == 1 and roles(out) == ['user', 'assistant', 'user', 'assistant', 'user']
assert out[2]['content'] == [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]

# 3. text-only tool results and plain user images are untouched
tr3 = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': [{'type': 'text', 'text': 'x'}]}]}
plain = {'role': 'user', 'content': [IMG, {'type': 'text', 'text': 'look'}]}
src = [plain, call, tr3]
out, moved = rewrite_messages(src)
assert moved == 0 and out == src

# 4. body level: all models (including anthropic.* on Vertex AI) are rewritten and protected from 30MB limits
for model in ('anthropic.claude-sonnet-5-5', 'anthropic.claude-opus-5-5', 'claude-argon'):
    raw = json.dumps({'model': model, 'messages': [ask, call, tr]}).encode()
    new, m, mv = rewrite_body(raw)
    assert mv == 1 and new != raw, (model, mv)

# 5. invalid JSON passes through
assert rewrite_body(b'not json') == (b'not json', None, 0)

# 6. media pruning: older media beyond max_recent is pruned
many_msgs = [ask]
for i in range(10):
    many_msgs.append({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f't{i}', 'name': 'Read', 'input': {}}]})
    many_msgs.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f't{i}', 'content': [IMG]}]})
out_pruned, moved_pruned = rewrite_messages(many_msgs, max_recent=3)
assert moved_pruned == 3, f'Expected 3 moved, got {moved_pruned}'
# Verify older images replaced by text notes
assert any('previously inspected' in json.dumps(m) for m in out_pruned)
# Verify strict alternation: no consecutive identical roles
pr_roles = roles(out_pruned)
assert not any(pr_roles[i] == pr_roles[i+1] for i in range(len(pr_roles)-1)), pr_roles

# 6b. current turn media is NEVER pruned even if exceeding max_recent
batch_msgs = [ask, {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tb', 'name': 'Read', 'input': {}}]}]
batch_msgs.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tb', 'content': [IMG]*8}]})
out_batch, moved_batch = rewrite_messages(batch_msgs, max_recent=3)
assert moved_batch == 8, f'Expected 8 moved, got {moved_batch}'
assert not any('previously inspected' in json.dumps(m) for m in out_batch), 'Current turn should never be pruned'

# 7. token calculation
from proxy import calculate_approx_tokens
tokens, mdl = calculate_approx_tokens(json.dumps({'model': 'claude-argon', 'messages': [ask, call, tr]}))
assert tokens > 1000 and mdl == 'claude-argon', (tokens, mdl)

# 8. multi-tool use with consecutive assistant and user turns (preserves matching functionResponse pairing)
multi_msgs = [
    {"role": "user", "content": "do tasks"},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    {"role": "user", "content": [{"type": "text", "text": "scaling note"}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": [IMG]}]}
]
out_multi, moved_multi = rewrite_messages(multi_msgs)
assert moved_multi == 1
assert roles(out_multi) == ["user", "assistant", "user", "assistant", "user"]
assert len(out_multi[1]["content"]) == 2
assert [b["tool_use_id"] for b in out_multi[2]["content"]] == ["t1", "t2"]

# 9. thinking block sanitization for anthropic models (prevents 400 invalid signature on model switch)
anthropic_msgs = [
    ask,
    {'role': 'assistant', 'content': [
        {'type': 'thinking', 'thinking': 'reasoning from gemini', 'signature': ''},
        {'type': 'text', 'text': 'final text'}
    ]}
]
raw_anthropic = json.dumps({'model': 'anthropic.claude-opus-5-5', 'messages': anthropic_msgs}).encode()
new_body, m, mv = rewrite_body(raw_anthropic)
parsed = json.loads(new_body)
assert not any(b.get('type') == 'thinking' for b in parsed['messages'][1]['content']), 'Thinking block should be stripped for Anthropic model'
assert any(b.get('text') == 'final text' for b in parsed['messages'][1]['content'])

# 10. historical tool result pruning to prevent 1M token / 30MB payload limits
from proxy import prune_historical_tool_results
oversized_msgs = []
for i in range(50):
    oversized_msgs.append({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f't{i}', 'name': 'bash', 'input': {}}]})
    oversized_msgs.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f't{i}', 'content': 'X' * 5000}]})
pruned_msgs, trunc_cnt, saved_chars = prune_historical_tool_results(oversized_msgs, keep_recent_msgs=20, max_chars=1000)
assert trunc_cnt > 0, 'Should have truncated older tool results'
assert saved_chars > 50000, f'Expected large saved chars, got {saved_chars}'
# Most recent 20 messages must be untouched
assert oversized_msgs[-1]['content'][0]['content'] == 'X' * 5000
assert pruned_msgs[-1]['content'][0]['content'] == 'X' * 5000
# First message must be truncated with note
first_res = pruned_msgs[1]['content'][0]['content']
assert len(first_res) < 2000
assert 'truncated by proxy' in first_res

print('all rewrite tests passed')
