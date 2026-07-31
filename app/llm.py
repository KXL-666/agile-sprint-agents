import json
import time

import requests

from .security import decrypt_secret


DEEPSEEK_DEFAULT_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class ModelCallError(RuntimeError):
    pass


class _RetryableModelResponseError(ValueError):
    """A provider response that may succeed if requested again."""


def _provider_endpoint(provider):
    if not provider.enabled or not provider.encrypted_key:
        raise ModelCallError("尚未启用模型配置")
    if provider.provider == "DeepSeek":
        return (provider.base_url or DEEPSEEK_DEFAULT_URL).rstrip("/"), provider.model_name or DEEPSEEK_DEFAULT_MODEL
    base_url = provider.base_url.rstrip("/")
    model_name = provider.model_name
    if not base_url or not model_name:
        raise ModelCallError(f"请先为 {provider.provider} 填写兼容 OpenAI 的接口地址和模型名称")
    return base_url, model_name


def _extract_json_object(content):
    """Accept JSON in a code fence or a short explanatory wrapper, but never invent fields."""
    if not isinstance(content, str) or not content.strip():
        raise _RetryableModelResponseError("模型返回了空内容")
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Some OpenAI-compatible providers prepend a sentence before the JSON. Find
    # the first complete object while respecting quoted braces inside a string.
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            character = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    raise _RetryableModelResponseError("模型返回的 JSON 不完整或格式不正确")


def _request_json(endpoint, token, payload):
    response = requests.post(
        f"{endpoint}/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=75,
    )
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise _RetryableModelResponseError("模型没有返回 choices")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise _RetryableModelResponseError("模型输出达到长度上限，已自动申请更完整的输出")
    content = (choice.get("message") or {}).get("content")
    return _extract_json_object(content), body.get("usage") or {}


def _should_retry(error):
    if isinstance(error, _RetryableModelResponseError):
        return True
    if isinstance(error, requests.RequestException):
        response = getattr(error, "response", None)
        return response is None or response.status_code in RETRYABLE_STATUS_CODES
    return False


def _record_usage(provider, model_name, success, attempts, duration_ms, usage=None, error=""):
    """Persist provider-reported usage when available, without exposing API keys."""
    try:
        from flask import has_app_context
        if not has_app_context():
            return
        from .models import ModelUsage, db
        usage = usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
        db.session.add(ModelUsage(
            provider=provider.provider,
            model_name=model_name,
            success=success,
            attempts=attempts,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error_message=str(error)[:500] if error else "",
        ))
    except Exception:
        # Usage telemetry must never make the actual agent call fail.
        return


def complete(provider, system_prompt, user_prompt):
    """Call an OpenAI-compatible JSON endpoint with bounded reliability retries.

    The retry only asks the real model again. If all retries fail, callers still
    receive an error instead of a simulated agent result.
    """
    endpoint, model_name = _provider_endpoint(provider)
    token = decrypt_secret(provider.encrypted_key)
    is_file_proposal = "file_operations" in system_prompt or "file_operations" in user_prompt
    initial_max_tokens = 7000 if is_file_proposal else 2400
    last_error = None

    started_at = time.perf_counter()
    for attempt in range(MAX_ATTEMPTS):
        retry_notice = "" if attempt == 0 else (
            "\n上一次输出未能被系统解析。请只返回一个完整、闭合、可解析的 JSON object；"
            "不要使用 Markdown 代码块，不要省略字符串结尾，也不要附加解释文字。"
        )
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt + retry_notice},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.15 if attempt else 0.25,
            # File content can be longer than an ordinary task assignment.
            "max_tokens": min(12_000, initial_max_tokens * (2 if attempt else 1)),
            "response_format": {"type": "json_object"},
        }
        try:
            result, usage = _request_json(endpoint, token, payload)
            _record_usage(
                provider, model_name, True, attempt + 1,
                int((time.perf_counter() - started_at) * 1000), usage,
            )
            return result
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            last_error = error
            if attempt == MAX_ATTEMPTS - 1 or not _should_retry(error):
                break
            time.sleep(0.6 * (attempt + 1))

    _record_usage(
        provider, model_name, False, MAX_ATTEMPTS,
        int((time.perf_counter() - started_at) * 1000), error=last_error,
    )
    raise ModelCallError(f"模型调用失败（已自动重试 {MAX_ATTEMPTS} 次）：{last_error}") from last_error


def check_connection(provider):
    """A small, non-file-reading request used by the settings page."""
    endpoint, model_name = _provider_endpoint(provider)
    try:
        response = requests.post(
            f"{endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {decrypt_secret(provider.encrypted_key)}", "Content-Type": "application/json"},
            json={"model": model_name, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16, "temperature": 0},
            timeout=30,
        )
        response.raise_for_status()
        returned_model = response.json().get("model", model_name)
        return returned_model
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        raise ModelCallError(f"模型连接失败：{error}") from error
