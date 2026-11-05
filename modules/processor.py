from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from typing import Any

from loguru import logger
from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI, RateLimitError

from config import DeepSeekSettings, ProcessorSettings
from modules.collector import CollectedItem

_WHITESPACE = re.compile(r"\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NON_WORD = re.compile(r"[\W_]+")


DEFAULT_STYLES: tuple[str, ...] = (
    "casual remark that picks up one specific detail of the comment",
    "thoughtful and insightful follow-up question",
    "humorous, witty remark or light banter",
    "concise acknowledgement or brief nod",
    "relatable personal reaction or perspective",
)


ANTI_CLICHE_RULES = (
    "Never open with stock praise or agreement such as 'В точку!', 'Полностью согласен с автором!', "
    "'Spot on!', 'Great point!' or \"Couldn't agree more!\", and never sound like an assistant. "
    "Start with your own concrete reaction, the way an ordinary viewer would."
)

# Paraphrases whose character-3gram Jaccard overlap reaches this value count as the same text
# (measured: copies 0.9+, real paraphrases 0.2-0.55, unrelated comments below 0.2).
SIMILARITY_LIMIT = 0.6
_RECENT_VARIANTS_MAX = 20
_RECENT_VARIANTS_IN_PROMPT = 5
_RETRY_HINT = (
    "\n\nThe previous rewrite was too similar to the original or to the used ones. "
    "Use a clearly different sentence structure and vocabulary."
)

PARAPHRASE_SYSTEM_PROMPT = (
    "You rewrite a short YouTube comment so that it says exactly the same thing in different words. "
    "Keep the language, tone, approximate length, emoji, links and every @mention unchanged. "
    "Do not add or drop facts. Reply with the rewritten comment only, without quotes or explanations."
)
_MENTION = re.compile(r"@[\w.\-]+")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _trigrams(text: str) -> set[str]:
    padded = f" {_NON_WORD.sub(' ', text.lower()).strip()} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)} if len(padded) > 2 else set()


def text_similarity(first: str, second: str) -> float:
    """Jaccard overlap of character 3-grams (case and punctuation ignored): 1.0 for copies, ~0 for unrelated text."""
    a, b = _trigrams(first), _trigrams(second)
    if not a or not b:  # emoji- or punctuation-only text has no trigrams: identical text still counts as a copy
        return 1.0 if first.strip().lower() == second.strip().lower() and first.strip() else 0.0
    return len(a & b) / len(a | b)


_LANGUAGE_NAMES = {
    "ru": "Russian", "uk": "Ukrainian", "be": "Belarusian", "kk": "Kazakh", "en": "English", "es": "Spanish",
    "pt": "Portuguese", "de": "German", "fr": "French", "it": "Italian", "tr": "Turkish", "pl": "Polish",
    "ar": "Arabic", "hi": "Hindi", "id": "Indonesian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
}


def _language_rule(code: str) -> str:
    """Hard instruction that keeps the reply in the comment's language (an English prompt makes DeepSeek drift to English)."""
    name = _LANGUAGE_NAMES.get((code or "").lower())
    if not name:
        return "Reply in the exact same language as the comment above, even if these instructions are in English."
    return (
        f"The comment is written in {name}. Write the reply ONLY in {name}"
        + ("" if name == "English" else ", never in English")
        + "."
    )


def _forced_language_rule(code: str) -> str:
    """Rule for a language fixed in settings: the reply is in it whatever language the comment is in."""
    name = _LANGUAGE_NAMES.get(code, code.upper())
    return f"Write the reply ONLY in {name}, regardless of the language of the comment" + (
        ", never in English." if code != "en" else "."
    )


class ProcessorError(RuntimeError):
    """Raised when the DeepSeek integration cannot produce usable text."""


def should_paraphrase(api_settings: Any, text_settings: Any) -> bool:
    """True when custom templates should be reworded by DeepSeek (switched on and a key is set)."""
    return bool(getattr(text_settings, "paraphrase_custom", False) and getattr(api_settings, "api_key", ""))


def resolve_reply_text(
    item: CollectedItem,
    *,
    custom_reply: str | None = None,
    custom_replies: list[str] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Resolve and format a custom user reply without calling the LLM."""
    generator = rng or random.Random()
    valid_replies = [r.strip() for r in (custom_replies or []) if r and r.strip()]
    if custom_reply and custom_reply.strip():
        template = custom_reply.strip()
    elif valid_replies:
        template = generator.choice(valid_replies).strip()
    else:
        raise ProcessorError("No custom reply text provided and custom_replies list is empty")

    t = template

    # Safe placeholder substitutions
    replacements = {
        "{text}": item.text,
        "{parent_text}": item.parent_text or "",
        "{language}": item.language.upper(),
    }
    for tag, val in replacements.items():
        t = t.replace(tag, str(val))

    formatted = t.strip()
    if not formatted:
        raise ProcessorError("Custom reply resolved to an empty string")
    return formatted


def normalize_text(
    text: str,
    *,
    rng: random.Random | None = None,
    drop_trailing_period_probability: float = 0.8,
    lowercase_initial_probability: float = 0.2,
    max_sentences: int = 2,
) -> str:
    """Apply the fixture typography rules used across the test suite."""
    if not text:
        return ""
    if not 0 <= drop_trailing_period_probability <= 1:
        raise ValueError("drop_trailing_period_probability must be within [0, 1]")
    if not 0 <= lowercase_initial_probability <= 1:
        raise ValueError("lowercase_initial_probability must be within [0, 1]")
    if max_sentences is not None and (not isinstance(max_sentences, int) or isinstance(max_sentences, bool) or max_sentences < 1):
        raise ValueError("max_sentences must be positive integer")

    generator = rng or random.Random()
    result = text.replace("\u2014", "-").replace("\u2013", "-")
    result = result.replace("\u00ab", '"').replace("\u00bb", '"')
    result = _WHITESPACE.sub(" ", result).strip()
    if not result:
        return ""

    if max_sentences is not None:
        sentences = _SENTENCE_SPLIT.split(result)
        if len(sentences) > max_sentences:
            result = " ".join(sentences[:max_sentences])

    if result.endswith(".") and not result.endswith("...") and generator.random() < drop_trailing_period_probability:
        result = result[:-1].rstrip()

    if generator.random() < lowercase_initial_probability:
        for index, character in enumerate(result):
            if character.isalpha():
                result = result[:index] + character.lower() + result[index + 1 :]
                break

    return result


class DeepSeekProcessor:
    """Generates short fixture texts through the DeepSeek chat completions API."""

    def __init__(
        self,
        api_settings: DeepSeekSettings,
        text_settings: ProcessorSettings,
        rng: random.Random | None = None,
    ) -> None:
        self._api = api_settings
        self._text = text_settings
        self._rng = rng or random.Random()
        self._recent_variants: deque[str] = deque(maxlen=_RECENT_VARIANTS_MAX)
        self.fallback_count = 0
        self._client = AsyncOpenAI(
            api_key=api_settings.api_key,
            base_url=api_settings.base_url,
            timeout=api_settings.timeout_seconds,
            max_retries=0,
        )

    async def close(self) -> None:
        await self._client.close()

    async def paraphrase(self, text: str) -> str:
        """Reword `text` keeping its meaning, with one retry when the result is unusable or a near-copy.

        A variant is unusable when it is empty, too long, drops a mention, or reaches SIMILARITY_LIMIT against the
        template or a recently produced variant. When no usable variant comes out (API error, two bad results) the
        template itself is returned and logged loudly; callers still post it.
        """
        mentions = [m.rstrip(".-") for m in _MENTION.findall(text)]
        limit = max(self._text.max_output_length, int(len(text) * 1.5))
        # The template's language wins, whatever language the comment under it is in; never translate.
        forced = getattr(self._text, "reply_language", "auto")
        if forced != "auto":
            name = _LANGUAGE_NAMES.get(forced, forced.upper())
            template_rule = f"Write the rewrite ONLY in {name}. If the text is in another language, translate it into {name}."
        elif _CYRILLIC.search(text):
            template_rule = "The text is in Russian. Write the rewrite ONLY in Russian, never in English, and never translate it."
        else:
            template_rule = "Write the rewrite in the exact same language as the text. Never translate it."
        for attempt in (1, 2):
            prompt = f"Comment:\n{text}"
            if self._recent_variants:
                used = list(self._recent_variants)[-_RECENT_VARIANTS_IN_PROMPT:]
                prompt += "\n\nAlready used, choose different wording:\n" + "\n".join(f"- {v}" for v in used)
            if attempt == 2:
                prompt += _RETRY_HINT
            try:
                response = await self._client.chat.completions.create(
                    model=self._api.model,
                    messages=[
                        {"role": "system", "content": f"{PARAPHRASE_SYSTEM_PROMPT}\n\n{template_rule}"},
                        {"role": "user", "content": f"{prompt}\n\n{template_rule}"},
                    ],
                    temperature=max(float(self._api.temperature), 0.9),
                    max_tokens=self._api.max_tokens,
                    stream=False,
                )
            except Exception as exc:
                logger.warning("Перефразирование не удалось ({})", exc)
                break

            content = self._extract_text(response)
            variant = _WHITESPACE.sub(" ", content.strip().strip("\"'«»“”")).strip() if content else ""
            if not variant or len(variant) > limit or any(m not in variant for m in mentions):
                logger.warning("Перефразирование дало непригодный текст (попытка {}/2)", attempt)
                continue
            # No await between this check and the append: parallel workers always see each other's finished variants.
            if any(text_similarity(variant, other) >= SIMILARITY_LIMIT for other in (text, *self._recent_variants)):
                logger.warning("Перефразирование слишком похоже на шаблон или недавний ответ (попытка {}/2)", attempt)
                continue
            self._recent_variants.append(variant)
            return variant

        self.fallback_count += 1
        logger.error(
            "Перефразирование не дало уникального текста: шаблон уйдёт БЕЗ ИЗМЕНЕНИЙ (случай №{}); "
            "добавьте больше разных шаблонов в custom_replies",
            self.fallback_count,
        )
        self._recent_variants.append(text)  # later paraphrases must not echo what is about to be posted
        return text

    async def generate(self, item: CollectedItem, *, style: str | None = None) -> str:
        if not item.text.strip():
            raise ProcessorError("cannot generate text for an empty source item")
        chosen_style = style
        if not chosen_style:
            available_styles = getattr(self._text, "styles", None) or DEFAULT_STYLES
            chosen_style = self._rng.choice(available_styles)

        forced = getattr(self._text, "reply_language", "auto")
        prompt_language = (item.language if forced == "auto" else forced).upper()
        prompt_kwargs = {
            "text": item.text,
            "language": prompt_language,
            "parent_text": item.parent_text or "",
            "parent_author": item.parent_author or "",
            "author": item.author or "",
            "style": chosen_style or "conversational and natural",
        }
        try:
            prompt = self._text.user_prompt_template.format(**prompt_kwargs)
        except KeyError:
            prompt = self._text.user_prompt_template.format(text=item.text, language=prompt_language)

        if item.parent_text and "{parent_text}" not in self._text.user_prompt_template:
            prompt = f"Context (parent comment): {item.parent_text}\nTarget sub-comment to reply to: {item.text}\n" + prompt

        if "{style}" not in self._text.user_prompt_template and chosen_style:
            prompt = f"{prompt}\nTone/Style: {chosen_style}"

        language_rule = _language_rule(item.language) if forced == "auto" else _forced_language_rule(forced)
        messages = [
            {"role": "system", "content": f"{self._text.system_prompt.strip()}\n\n{ANTI_CLICHE_RULES}\n\n{language_rule}"},
            {"role": "user", "content": f"{prompt.strip()}\n\n{language_rule}"},
        ]

        last_error = "unknown error"
        for attempt in range(1, self._api.max_retries + 1):
            started = time.perf_counter()
            try:
                response = await self._client.chat.completions.create(
                    model=self._api.model,
                    messages=messages,
                    temperature=self._api.temperature,
                    max_tokens=self._api.max_tokens,
                    stream=False,
                )
            except RateLimitError as exc:
                last_error = f"rate limited: {exc}"
                retryable = True
            except APIConnectionError as exc:
                last_error = f"connection error: {exc}"
                retryable = True
            except APIStatusError as exc:
                last_error = f"HTTP {exc.status_code}: {exc}"
                retryable = exc.status_code == 429 or exc.status_code >= 500
                if not retryable:
                    raise ProcessorError(f"DeepSeek rejected the request: {last_error}") from exc
            except APIError as exc:
                raise ProcessorError(f"DeepSeek request failed: {exc}") from exc
            else:
                content = self._extract_text(response)
                if content is None:
                    last_error = "empty completion"
                    retryable = True
                else:
                    text = normalize_text(
                        content,
                        rng=self._rng,
                        drop_trailing_period_probability=self._text.drop_trailing_period_probability,
                        lowercase_initial_probability=self._text.lowercase_initial_probability,
                        max_sentences=self._text.max_sentences,
                    )
                    if len(text) > self._text.max_output_length:
                        text = self._trim_to_length(text, self._text.max_output_length)
                    if not text:
                        last_error = "completion became empty after normalization"
                        retryable = True
                    else:
                        logger.debug(
                            "DeepSeek сгенерировал ответ ({} симв.) за {:.2f}с (попытка {}/{})",
                            len(text),
                            time.perf_counter() - started,
                            attempt,
                            self._api.max_retries,
                        )
                        return text

            if not retryable:
                raise ProcessorError(f"Ошибка обращения к DeepSeek API: {last_error}")
            if attempt < self._api.max_retries:
                delay = min(2.0 * attempt, 15.0) + self._rng.uniform(0, 0.5)
                logger.warning(
                    "Попытка DeepSeek {}/{} не удалась ({}); повтор через {:.1f}с",
                    attempt,
                    self._api.max_retries,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        raise ProcessorError(f"DeepSeek failed after {self._api.max_retries} attempts: {last_error}")

    def _extract_text(self, response: Any) -> str | None:
        choices = getattr(response, "choices", None)
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            return None
        content = content.strip()
        if not content:
            return None
        return content

    @staticmethod
    def _trim_to_length(text: str, limit: int) -> str:
        cut = text[:limit]
        boundary = cut.rfind(" ")
        if boundary > limit // 2:
            cut = cut[:boundary]
        return cut.rstrip(" ,;:-")


__all__ = [
    "ANTI_CLICHE_RULES",
    "DEFAULT_STYLES",
    "DeepSeekProcessor",
    "ProcessorError",
    "normalize_text",
    "resolve_reply_text",
    "should_paraphrase",
    "text_similarity",
]
