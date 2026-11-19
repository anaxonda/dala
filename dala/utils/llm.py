import os
import aiohttp
import asyncio
from typing import Any, Dict, Optional
from ..models import log

LLM_MODEL_PRESETS = {
    "gemini": (
        ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
        ("gemini-4-hydrogen", "Gemini 4 Hydrogen"),
        ("gemini-4-neon", "Gemini 4 Neon"),
        ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ),
    "openrouter": (
        ("deepseek/deepseek-v4-flash-0731", "DeepSeek V4 Flash 0731"),
        ("google/gemini-4-hydrogen", "Gemini 4 Hydrogen"),
        ("google/gemini-4-neon", "Gemini 4 Neon"),
        ("openai/gpt-6-luna", "GPT-6 Luna"),
    ),
    "openai": (
        ("gpt-6-luna", "GPT-6 Luna"),
        ("gpt-6.2-sol", "GPT-6.2 Sol"),
        ("gpt-6.2-astra", "GPT-6.2 Astra"),
    ),
}
DEFAULT_LLM_MODELS = {provider: presets[0][0] for provider, presets in LLM_MODEL_PRESETS.items()}


def normalize_llm_provider(provider: Optional[str]) -> str:
    value = (provider or "auto").strip().lower()
    return value if value in {"auto", "gemini", "openrouter", "openai"} else "auto"


def infer_llm_provider(model: Optional[str], provider: Optional[str], has_gemini: bool, has_openrouter: bool, has_openai: bool) -> str:
    selected = normalize_llm_provider(provider)
    if selected != "auto":
        return selected
    model_id = (model or "").strip().lower()
    if model_id.startswith("gemini-"):
        return "gemini"
    if "/" in model_id:
        if model_id.startswith("openai/") and has_openai and not has_openrouter:
            return "openai"
        return "openrouter"
    if model_id.startswith(("gpt-", "chatgpt-")):
        return "openai"
    if has_gemini:
        return "gemini"
    if has_openrouter:
        return "openrouter"
    if has_openai:
        return "openai"
    return "auto"


def api_key_provider(api_key: str) -> Optional[str]:
    if api_key.startswith("AIza"):
        return "gemini"
    if api_key.startswith("sk-or-"):
        return "openrouter"
    if api_key.startswith("sk-"):
        return "openai"
    return None


class LLMHelper:
    @staticmethod
    async def _call_llm(
        prompt: str,
        model: Optional[str],
        api_key: Optional[str],
        provider: Optional[str] = None,
        request_options: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        # Requests that supply a provider (including Auto) use its model default.
        model = model or (os.getenv("LLM_MODEL") if provider is None else None)
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        requested_provider = provider or os.getenv("LLM_PROVIDER")
        keys = {name: os.getenv(f"{name.upper()}_API_KEY") for name in DEFAULT_LLM_MODELS}
        if api_key:
            override = api_key_provider(api_key) or infer_llm_provider(model, requested_provider, False, False, False)
            keys = {"openrouter" if override == "auto" else override: api_key}
        selected_provider = infer_llm_provider(
            model, requested_provider, bool(keys.get("gemini")), bool(keys.get("openrouter")), bool(keys.get("openai")),
        )
        active_key = keys.get(selected_provider)
        if not active_key:
            log.warning("No matching API key for LLM provider %s. Skipping LLM task.", selected_provider)
            return None
        target_model = model or DEFAULT_LLM_MODELS[selected_provider]
        request_options = request_options or {}

        try:
            # Google Gemini (REST)
            if selected_provider == "gemini":
                if "/" in target_model:
                    log.error("Gemini provider cannot use non-Gemini model id: %s", target_model)
                    return None
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                generation_config = request_options.get("gemini_generation_config") or {}
                if generation_config:
                    payload["generationConfig"] = generation_config
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers={"x-goog-api-key": active_key}, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if 'candidates' in data and data['candidates']:
                                parts = data['candidates'][0].get('content', {}).get('parts', [])
                                return "".join(part.get('text', '') for part in parts if not part.get('thought')) or None
                            else:
                                log.warning(f"Gemini API returned no candidates: {data}")
                                return None
                        else:
                            log.error(f"Gemini API Error: {resp.status} {await resp.text()}")
                            return None

            # OpenAI Compatible (OpenRouter / OpenAI)
            use_openrouter = selected_provider == "openrouter"
            base_url = "https://openrouter.ai/api/v1" if use_openrouter else "https://api.openai.com/v1"
            if not use_openrouter:
                target_model = target_model.removeprefix("openai/")
                if "/" in target_model:
                    log.error("OpenAI provider cannot use router model id: %s", target_model)
                    return None

            headers = {
                "Authorization": f"Bearer {active_key}",
                "Content-Type": "application/json"
            }
            if use_openrouter:
                headers["HTTP-Referer"] = "https://github.com/loki/dala"
                headers["X-Title"] = "EPUB Downloader"

            payload = {
                "model": target_model,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."}, 
                    {"role": "user", "content": prompt}
                ]
            }
            payload.update(request_options.get("chat_payload", {}))
            if use_openrouter:
                payload.update(request_options.get("openrouter_payload", {}))
            elif target_model.startswith("gpt-6"):
                for parameter in ("temperature", "top_p", "logprobs", "top_logprobs"):
                    payload.pop(parameter, None)
                if "max_tokens" in payload:
                    payload.setdefault("max_completion_tokens", payload.pop("max_tokens"))
                payload.setdefault("reasoning_effort", "none" if target_model.startswith("gpt-6-luna") else "low")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                    else:
                        log.error(f"LLM API Error ({base_url}): {resp.status} {await resp.text()}")
                        return None

        except Exception as e:
            log.error(f"LLM call failed: {e}")
            return None

    @staticmethod
    async def format_transcript(
        text: str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        # Prepare Prompt
        custom_prompt = os.getenv("LLM_PROMPT")
        if custom_prompt:
            prompt = custom_prompt.replace("{text}", text)
        else:
            prompt = (
                "You are an expert editor. Please format the following YouTube transcript into a readable article. "
                "Fix punctuation, capitalization, and paragraph breaks. "
                "Do not summarize; keep the full content but make it flow like a written piece. "
                "Remove filler words like 'um', 'uh', 'like' where appropriate. "
                "Format the output as HTML (using <p> tags for paragraphs).\n\n"
                f"{text}"
            )
        
        result = await LLMHelper._call_llm(prompt, model, api_key, provider=provider)
        return result if result else text

    @staticmethod
    async def generate_summary(
        text: str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Optional[str]:
        custom_prompt = os.getenv("LLM_SUMMARY_PROMPT")
        if custom_prompt:
            prompt = custom_prompt.replace("{text}", text[:15000]) # truncate to avoid huge context costs?
        else:
            prompt = (
                "Please provide a concise executive summary (3-5 paragraphs) of the following text. "
                "Capture the main arguments, key takeaways, and conclusion. "
                "Format the response as valid HTML (using <p>, <strong>, <ul>/<li> tags). "
                "Do NOT use Markdown syntax (like ** or ###).\n\n"
                f"{text[:25000]}" # Limit context window usage for summary
            )
        
        return await LLMHelper._call_llm(prompt, model, api_key, provider=provider)
