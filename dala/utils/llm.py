import os
import aiohttp
import asyncio
from typing import Optional
from . .models import log

class LLMHelper:
    @staticmethod
    async def _call_llm(prompt: str, model: Optional[str], api_key: Optional[str]) -> Optional[str]:
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

        # Determine Model
        model = model or os.getenv("LLM_MODEL")

        try:
            # Google Gemini (REST)
            if gemini_key:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or 'gemini-1.5-flash'}:generateContent?key={gemini_key}"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
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
                            if not (openrouter_key or openai_key): return None

            # OpenAI Compatible (OpenRouter / OpenAI)
            active_key = openrouter_key or openai_key
            base_url = "https://openrouter.ai/api/v1" if openrouter_key and not openai_key else "https://api.openai.com/v1"
            if "openrouter" in (model or ""): base_url = "https://openrouter.ai/api/v1"
            
            if api_key and not passed_key_is_gemini:
                active_key = api_key
                if model and ("/" in model): base_url = "https://openrouter.ai/api/v1"

            target_model = model or ("google/gemini-flash-1.5" if "openrouter" in base_url else "gpt-3.5-turbo")
            
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
    async def format_transcript(text: str, model: Optional[str] = None, api_key: Optional[str] = None) -> str:
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
        
        result = await LLMHelper._call_llm(prompt, model, api_key)
        return result if result else text

    @staticmethod
    async def generate_summary(text: str, model: Optional[str] = None, api_key: Optional[str] = None) -> Optional[str]:
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
        
        return await LLMHelper._call_llm(prompt, model, api_key)
