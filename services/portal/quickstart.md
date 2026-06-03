# GaaS Gateway Quickstart

## Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://gateway.internal/v1",
    api_key="sk-agent-your-virtual-key"
)

response = client.chat.completions.create(
    model="smart-agent-route", # Let the gateway decide!
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

## cURL

```bash
curl https://gateway.internal/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-agent-your-virtual-key" \
  -d '{
    "model": "smart-agent-route",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```
