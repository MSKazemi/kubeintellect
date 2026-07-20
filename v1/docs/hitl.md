# Human-in-the-Loop (HITL) Workflow Guide for KubeIntellect

## Overview

KubeIntellect has two levels of human confirmation:

| Level | Trigger | Mechanism |
|-------|---------|-----------|
| **CodeGenerator HITL** | Workflow routes to CodeGenerator to synthesize new code | LangGraph `interrupt_before=["CodeGenerator"]` — full checkpoint/resume cycle |
| **Deletion confirmation** | Deletion agent is about to delete a resource | Inline agent message — the Deletion agent asks "I'm about to delete X in namespace Y — confirm?" before calling any delete tool. No checkpoint needed. |

This section covers the **CodeGenerator HITL** flow. Deletion confirmation requires no special API handling — the user simply replies to the agent's confirmation message in the normal chat flow.

## CodeGenerator HITL

The workflow pauses before invoking the CodeGenerator agent and requests explicit human approval. This ensures that new code generation only happens with user consent, enhancing security and control.

## How It Works

### 1. Automatic Pause Before Code Generation

When the workflow determines that it needs to generate new code/tools using the CodeGenerator agent, it will:
- Automatically pause execution
- Save the current state to PostgreSQL for later resume
- Return a breakpoint message to the user requesting approval

### 2. User Approval/Denial

The user can then:
- **Approve**: Continue with code generation
- **Deny**: Stop the workflow for security reasons

### 3. Resume or Cleanup

Based on user response:
- **If approved**: Workflow resumes from the saved checkpoint
- **If denied**: Checkpoint is cleaned up and workflow ends

## API Usage

### Step 1: Normal Chat Request with User ID

To enable HITL, include a `user` field in your chat completion request:

```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {
      "role": "user",
      "content": "Create a tool to list all pods in the kube-system namespace"
    }
  ],
  "stream": false,
  "user": "user123"
}
```

### Step 2: Handle Breakpoint Response

If the workflow needs to generate code, you'll receive a response like:

```json
{
  "id": "chatcmpl-xxxxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "gpt-3.5-turbo",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "🛑 **APPROVAL REQUIRED**\n\nThe workflow wants to generate new code/tools using the CodeGenerator agent. This will create new functionality in your system.\n\n**To approve:** Type 'approve', 'yes', or 'continue'\n**To deny:** Type 'deny', 'no', or 'cancel'\n\nDo you approve this action?"
      },
      "finish_reason": "stop"
    }
  ]
}
```

### Step 3: Approve or Deny

You have **two options** to respond:

#### Option A: Text-Based Approval (Recommended)

Simply send another chat message with approval/denial keywords:

**To Approve:**
```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {
      "role": "user",
      "content": "approve"
    }
  ],
  "user": "user123"
}
```

**To Deny:**
```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {
      "role": "user",
      "content": "deny"
    }
  ],
  "user": "user123"
}
```

**Supported Keywords:**
- **Approval:** `approve`, `approved`, `yes`, `y`, `continue`, `proceed`, `ok`, `okay`
- **Denial:** `deny`, `denied`, `no`, `n`, `reject`, `cancel`, `stop`

#### Option B: Direct API Call

Make a POST request to the HITL resume endpoint:

**Approve the Action:**
```bash
curl -X POST "http://localhost:8000/api/v1/chat/hitl/resume" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "approved": true,
    "original_query": "Create a tool to list all pods in the kube-system namespace"
  }'
```

**Deny the Action:**
```bash
curl -X POST "http://localhost:8000/api/v1/chat/hitl/resume" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "approved": false
  }'
```

## SSE Event Schema (authoritative server/CLI contract)

When a HITL breakpoint fires in the streaming path the server emits **two** consecutive SSE events:

**Event 1 — breakpoint content** (displays approval request text to the user):
```json
{
  "id": "chatcmpl-<uuid>",
  "object": "chat.completion.chunk",
  "choices": [{
    "index": 0,
    "delta": { "role": "assistant", "content": "\n\n🛑 **APPROVAL REQUIRED**\n\n..." },
    "finish_reason": "stop",
    "hitl_required": false,
    "action_id": null
  }]
}
```

**Event 2 — HITL signal** (no content; carries structured fields for the CLI):
```json
{
  "id": "chatcmpl-<uuid>",
  "object": "chat.completion.chunk",
  "choices": [{
    "index": 0,
    "delta": {},
    "finish_reason": "stop",
    "hitl_required": true,
    "action_id": "3f2a8c1e-0000-0000-0000-000000000000"
  }]
}
```

**`hitl_required`** — `true` on the signal event. CLI must use this field to detect HITL. Emoji scanning (`"🛑" in text`) is a deprecated fallback for old server versions.

**`action_id`** — UUID generated per breakpoint. CLI stores this in `SessionState.pending_action_id` and sends it back in the approve/deny request body so the server can write a correlated row to `audit_log(action_id, decision)`.

**Non-streaming path:** The same fields appear on the `Choice` object in the `ChatCompletionResponse`:
```json
{
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "\n\n🛑 **APPROVAL REQUIRED**\n\n..." },
    "finish_reason": "stop",
    "hitl_required": true,
    "action_id": "3f2a8c1e-0000-0000-0000-000000000000"
  }]
}
```

**Approve/deny request:** CLI includes `action_id` in the request body when sending the approval message:
```json
{
  "model": "gpt-3.5-turbo",
  "messages": [{ "role": "user", "content": "approve" }],
  "conversation_id": "<conversation-id>",
  "user_id": "<user-id>",
  "action_id": "3f2a8c1e-0000-0000-0000-000000000000"
}
```

---

## Streaming Support

HITL also works with streaming requests. When a breakpoint is reached during streaming:

```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {
      "role": "user", 
      "content": "Generate a tool to get deployment status"
    }
  ],
  "stream": true,
  "user": "user123"
}
```

The stream will include the breakpoint message and then end, requiring the same resume process.

## Frontend Integration

For web frontends (like LibreChat), you can:

1. **Detect Breakpoint Messages**: Look for messages containing "🛑 **APPROVAL REQUIRED**"
2. **Show Approval Dialog**: Present an approve/deny dialog to the user
3. **Call Resume Endpoint**: Based on user choice, call the `/chat/hitl/resume` endpoint
4. **Display Results**: Show the final result to the user

### Example Frontend Flow

```javascript
// 1. Send normal chat request with user ID
const response = await fetch('/api/v1/chat/completions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    model: 'gpt-3.5-turbo',
    messages: [{ role: 'user', content: userQuery }],
    user: userId
  })
});

const result = await response.json();

// 2. Check if it's a breakpoint
if (result.choices[0].message.content.includes('🛑 **APPROVAL REQUIRED**')) {
  // 3. Show approval dialog
  const approved = await showApprovalDialog(result.choices[0].message.content);
  
  // 4. Resume workflow
  const resumeResponse = await fetch('/api/v1/chat/hitl/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      approved: approved,
      original_query: userQuery
    })
  });
  
  const finalResult = await resumeResponse.json();
  // Display final result
}
```

## Configuration

### PostgreSQL Requirements

HITL requires PostgreSQL for checkpoint storage:
- PostgreSQL server must be accessible at `postgres.kubeintellect.svc.cluster.local:5432`
- Checkpoints are stored with 1-hour TTL
- If PostgreSQL is unavailable, HITL will be disabled gracefully

### Security Considerations

1. **User Authentication**: Ensure `user_id` values are properly authenticated
2. **Checkpoint Cleanup**: Checkpoints are automatically cleaned up after 1 hour or upon completion/denial
3. **Code Review**: Even with approval, consider reviewing generated code before deployment

## Troubleshooting

### Common Issues

1. **No PostgreSQL Connection**: HITL will be disabled, check PostgreSQL connectivity
2. **Missing User ID**: HITL won't work without a user ID in the request
3. **Checkpoint Expired**: Checkpoints expire after 1 hour, user needs to restart

### Logs to Monitor

- `[HITL] Workflow interrupted before CodeGenerator` - Breakpoint triggered
- `[HITL] Saved HITL checkpoint` - State saved successfully  
- `[HITL] Restored checkpoint` - Resume successful
- `PostgreSQL not available for HITL checkpointing` - PostgreSQL connectivity issues

## Example Complete Flow

1. **User asks**: "I need a tool to restart deployments"
2. **System responds**: Normal workflow starts, determines CodeGenerator is needed
3. **Breakpoint triggered**: Workflow pauses, saves state, returns approval request with clear instructions
4. **User types**: "approve" (or "yes", "continue", etc.)
5. **System acknowledges**: "✅ **Approved** - Resuming workflow..."
6. **Workflow continues**: CodeGenerator creates the tool, workflow completes
7. **Final response**: "I've created a tool to restart deployments. You can now use it by..."

## Alternative Denial Flow

1. **User asks**: "Create a dangerous tool"
2. **System responds**: Requests approval for CodeGenerator
3. **User types**: "deny" (or "no", "cancel", etc.)
4. **System responds**: "❌ **Action Denied** - The CodeGenerator action was denied by the user. The workflow has been stopped for security reasons."
5. **Workflow ends**: Checkpoint cleaned up, no code generated

This HITL system provides a secure way to handle dynamic code generation while maintaining user control and system security. 