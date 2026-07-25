"""长期记忆 service 单元测试 - 抽取结果解析 parse_extracted_memories。"""

from __future__ import annotations

import pytest

from starring.memory.service import MEMORY_CONTENT_MAX_LENGTH, parse_extracted_memories


@pytest.mark.unit
def test_parse_extracted_memories_valid_json_array() -> None:
    content = '["用户是一名 Python 后端工程师", "用户偏好简洁直接的回复风格"]'

    assert parse_extracted_memories(content) == [
        "用户是一名 Python 后端工程师",
        "用户偏好简洁直接的回复风格",
    ]


@pytest.mark.unit
def test_parse_extracted_memories_tolerates_code_block() -> None:
    content = '好的，抽取结果如下：\n```json\n["用户喜欢用中文交流"]\n```'

    assert parse_extracted_memories(content) == ["用户喜欢用中文交流"]


@pytest.mark.unit
def test_parse_extracted_memories_tolerates_plain_code_block() -> None:
    content = '```\n["用户是前端工程师"]\n```'

    assert parse_extracted_memories(content) == ["用户是前端工程师"]


@pytest.mark.unit
def test_parse_extracted_memories_empty_array_is_normal() -> None:
    assert parse_extracted_memories("[]") == []
    assert parse_extracted_memories("```json\n[]\n```") == []


@pytest.mark.unit
def test_parse_extracted_memories_invalid_json_raises() -> None:
    # json.JSONDecodeError 是 ValueError 子类，非法输出统一按 ValueError 处理
    with pytest.raises(ValueError):
        parse_extracted_memories("这不是 JSON")


@pytest.mark.unit
def test_parse_extracted_memories_non_list_raises() -> None:
    with pytest.raises(ValueError, match="期望 JSON 数组"):
        parse_extracted_memories('{"memory": "用户喜欢猫"}')


@pytest.mark.unit
def test_parse_extracted_memories_incomplete_code_block_raises() -> None:
    with pytest.raises(ValueError, match="代码块不完整"):
        parse_extracted_memories('```json\n["用户喜欢猫"]')


@pytest.mark.unit
def test_parse_extracted_memories_filters_non_string_and_blank_items() -> None:
    content = '["用户喜欢猫", 123, null, "  ", {"k": "v"}, " 用户喜欢狗 "]'

    assert parse_extracted_memories(content) == ["用户喜欢猫", "用户喜欢狗"]


@pytest.mark.unit
def test_parse_extracted_memories_truncates_long_items() -> None:
    long_item = "用" * (MEMORY_CONTENT_MAX_LENGTH + 100)
    content = f'["{long_item}"]'

    result = parse_extracted_memories(content)

    assert len(result) == 1
    assert len(result[0]) == MEMORY_CONTENT_MAX_LENGTH
