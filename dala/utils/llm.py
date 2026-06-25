import os
import aiohttp
import asyncio
from typing import Any, Dict, Optional
from ..models import log

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

OPENROUTER_MODEL_PREFIXES = (
    "anthropic/",
    "deepseek/",
    "google/",
    "meta-llama/",
    "mistralai/",
    "minimax/",
    "moonshotai/",
    "openai/",
    "qwen/",
    "x-ai/",
    "z-ai/",
)


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
    if "/" in model_id or model_id.startswith(OPENROUTER_MODEL_PREFIXES):
        if has_openrouter:
            return "openrouter"
        if model_id.startswith("openai/") and has_openai:
            return "openai"
    if has_gemini:
        return "gemini"
    if has_openrouter:
        return "openrouter"
    if has_openai:
        return "openai"
    return "auto"

class LLMHelper:
    @staticmethod
    async def _call_llm(
        prompt: str,
        model: Optional[str],
        api_key: Optional[str],
        provider: Optional[str] = None,
        request_options: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        gemini_key = api_key or os.getenv("GEMINI_API_KEY")
        openrouter_key = api_key or os.getenv("OPENROUTER_API_KEY")
        openai_key = api_key or os.getenv("OPENAI_API_KEY")

        # Heuristic to detect key type if passed generically
        passed_key_is_gemini = api_key and ("AIza" in api_key)
        
        if api_key:
            if passed_key_is_gemini:
                gemini_key = api_key
                openrouter_key = None
                openai_key = None
            else:
                gemini_key = None
                openrouter_key = api_key 
                openai_key = api_key

        if not (gemini_key or openrouter_key or openai_key):
            log.warning("No API keys found. Skipping LLM task.")
            return None

        # Determine Model. Legacy callers that do not pass a provider keep using LLM_MODEL.
        # Translation callers pass provider="auto" so server .env model defaults do not
        # accidentally force an OpenRouter model when Gemini is available.
        model = model or (os.getenv("LLM_MODEL") if provider is None else None)
        selected_provider = infer_llm_provider(model, provider or os.getenv("LLM_PROVIDER"), bool(gemini_key), bool(openrouter_key), bool(openai_key))
        request_options = request_options or {}

        try:
            # Google Gemini (REST)
            if selected_provider == "gemini":
                if not gemini_key:
                    log.error("Gemini provider selected but GEMINI_API_KEY is not configured.")
                    return None
                target_model = model or DEFAULT_GEMINI_MODEL
                if "/" in target_model:
                    log.error("Gemini provider cannot use non-Gemini model id: %s", target_model)
                    return None
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={gemini_key}"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                generation_config = request_options.get("gemini_generation_config") or {}
                if generation_config:
                    payload["generationConfig"] = generation_config
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if 'candidates' in data and data['candidates']:
                                return data['candidates'][0]['content']['parts'][0]['text']
                            else:
                                log.warning(f"Gemini API returned no candidates: {data}")
                                return None
                        else:
                            log.error(f"Gemini API Error: {resp.status} {await resp.text()}")
                            return None

            # OpenAI Compatible (OpenRouter / OpenAI)
            if selected_provider == "openrouter":
                active_key = openrouter_key
                base_url = "https://openrouter.ai/api/v1"
            elif selected_provider == "openai":
                active_key = openai_key
                base_url = "https://api.openai.com/v1"
            else:
                active_key = openrouter_key or openai_key
                base_url = "https://openrouter.ai/api/v1" if openrouter_key else "https://api.openai.com/v1"
            if not active_key:
                log.error("%s provider selected but no matching API key is configured.", selected_provider)
                return None
            if "openrouter" in (model or ""): base_url = "https://openrouter.ai/api/v1"
            
            if api_key and not passed_key_is_gemini:
                active_key = api_key
                if selected_provider == "openrouter" or (model and ("/" in model)):
                    base_url = "https://openrouter.ai/api/v1"

            target_model = model or (DEFAULT_OPENROUTER_MODEL if "openrouter" in base_url else DEFAULT_OPENAI_MODEL)
            
            headers = {
                "Authorization": f"Bearer {active_key}",
                "Content-Type": "application/json"
            }
            if "openrouter" in base_url:
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
            if "openrouter" in base_url:
                payload.update(request_options.get("openrouter_payload", {}))
            
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
