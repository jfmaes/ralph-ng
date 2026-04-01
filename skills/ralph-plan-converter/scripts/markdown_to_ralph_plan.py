#!/usr/bin/env python3
"""Convert a markdown implementation plan into a draft ralph-plan.yaml."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil


TASK_HEADING_RE = re.compile(r"^###\s+Task\s+(\d+):\s+(.+?)\s*$", re.MULTILINE)
SLICE_HEADING_RE = re.compile(r"^##\s+Slice\s+(\d+):", re.MULTILINE)
MAJOR_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)
BOLD_FIELD_RE = re.compile(r"^- \*\*(.+?):\*\*\s*(.*)$")
PLAIN_FIELD_RE = re.compile(r"^- ([A-Z][A-Za-z ]+?):\s*(.*)$")
TASK_REF_RE = re.compile(r"\bTask\s+(\d+)\b", re.IGNORECASE)
TASK_RANGE_RE = re.compile(r"\bTasks?\s+(\d+)\s*[–-]\s*(\d+)\b", re.IGNORECASE)
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
PATH_CANDIDATE_RE = re.compile(
    r"\b(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|md|toml|yaml|yml|json|txt|tsx|ts|js|jsx|css|html|sh)\b"
)
COMMAND_HINT_RE = re.compile(
    r"\b(?:pytest|python(?:3)?|uv|npm|pnpm|yarn|make|cargo|go\s+test|ruff|mypy|tox|bash|sh)\b"
)
TEST_PASS_HINT_RE = re.compile(r"\b(?:tests?|pytest)\b.*\bpass", re.IGNORECASE)


@dataclass(slots=True)
class TaskDraft:
    id: str
    title: str
    description: str
    depends_on: list[str]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    expected_files: list[str]
    slice_number: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Markdown implementation plan path")
    parser.add_argument("--output", type=Path, default=Path("ralph-plan.yaml"))
    parser.add_argument(
        "--spec",
        type=Path,
        default=None,
        help="Optional root spec markdown to copy into specs/",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root used for relative outputs",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path("specs"),
        help="Directory to place copied specs in, relative to --repo-root unless absolute",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.read_text(encoding="utf-8")
    tasks = parse_tasks(source)
    if not tasks:
        raise SystemExit("No tasks found. Expected markdown headings like '### Task 1: Title'.")

    tasks = add_slice_dependencies(tasks)

    output_path = args.output if args.output.is_absolute() else args.repo_root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_plan_yaml(tasks), encoding="utf-8")

    if args.spec is not None:
        copy_spec(args.spec, repo_root=args.repo_root, specs_dir=args.specs_dir)

    return 0


def parse_tasks(markdown: str) -> list[TaskDraft]:
    slice_ranges = parse_slice_ranges(markdown)
    matches = list(TASK_HEADING_RE.finditer(markdown))
    tasks: list[TaskDraft] = []
    errors: list[str] = []

    for index, match in enumerate(matches):
        start = match.start()
        end = find_task_end(markdown, matches, index)
        block = markdown[start:end].strip()
        task_number = int(match.group(1))
        title = match.group(2).strip()
        try:
            task = build_task(task_number, title, block)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        task.slice_number = slice_ranges.get(task_number, 0)
        tasks.append(task)

    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise SystemExit(f"Could not build a valid ralph draft from markdown:\n{joined}")

    return tasks


def find_task_end(markdown: str, matches: list[re.Match[str]], index: int) -> int:
    next_task = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
    current_start = matches[index].start()

    next_major = next_task
    for major_match in MAJOR_HEADING_RE.finditer(markdown, current_start + 1):
        candidate = major_match.start()
        if candidate < next_task:
            next_major = candidate
            break

    return min(next_task, next_major)


def parse_slice_ranges(markdown: str) -> dict[int, int]:
    slices = list(SLICE_HEADING_RE.finditer(markdown))
    task_matches = list(TASK_HEADING_RE.finditer(markdown))
    if not slices:
        return {}

    result: dict[int, int] = {}
    for task_match in task_matches:
        task_number = int(task_match.group(1))
        task_pos = task_match.start()
        current_slice = 0
        for slice_match in slices:
            if slice_match.start() <= task_pos:
                current_slice = int(slice_match.group(1))
        result[task_number] = current_slice
    return result


def add_slice_dependencies(tasks: list[TaskDraft]) -> list[TaskDraft]:
    if not any(task.slice_number > 0 for task in tasks):
        return tasks

    slices: dict[int, list[TaskDraft]] = {}
    for task in tasks:
        slices.setdefault(task.slice_number, []).append(task)

    slice_numbers = sorted(slices)
    for index in range(1, len(slice_numbers)):
        previous_slice = slice_numbers[index - 1]
        current_slice = slice_numbers[index]
        if previous_slice == 0:
            continue
        previous_task_id = slices[previous_slice][-1].id
        first_current = slices[current_slice][0]
        if previous_task_id not in first_current.depends_on:
            first_current.depends_on.append(previous_task_id)

    return tasks


def build_task(task_number: int, title: str, block: str) -> TaskDraft:
    task_id = f"TASK-{task_number:03d}"
    metadata, sections, sub_bullets = parse_block(block)

    depends_on = parse_dependencies(metadata.get("Depends on", ""))
    done_when_items = sub_bullets.get("Done when", [])
    done_when_single = metadata.get("Done when", "").strip()
    done_when_text = done_when_single or "\n".join(done_when_items)
    acceptance_criteria = done_when_items or ([done_when_single] if done_when_single else [])
    if not acceptance_criteria:
        raise ValueError(f"{task_id}: could not infer acceptance_criteria from markdown.")

    verification_commands = infer_verification_commands(
        test_first=metadata.get("Test first", ""),
        done_when=done_when_text,
        block=block,
        sections=sections,
    )
    if not verification_commands:
        raise ValueError(f"{task_id}: could not infer verification_commands from markdown.")

    expected_files = unique(
        extract_paths_from_section(sections.get("Create", []))
        + extract_paths_from_section(sections.get("Modify", []))
        + extract_paths_from_metadata(metadata.get("Create", ""))
        + extract_paths_from_metadata(metadata.get("Modify", ""))
    )
    if not expected_files:
        raise ValueError(f"{task_id}: could not infer expected_files from markdown.")

    description = build_description(block)
    return TaskDraft(
        id=task_id,
        title=title,
        description=description,
        depends_on=depends_on,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        expected_files=expected_files,
    )


def parse_block(block: str) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[str]]]:
    repeatable_fields = {"Create", "Modify"}

    metadata: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    sub_bullets: dict[str, list[str]] = {}
    current_section: str | None = None
    lines = block.splitlines()[1:]

    for raw_line in lines:
        line = raw_line.rstrip()

        bold_match = BOLD_FIELD_RE.match(line)
        if bold_match:
            field_name = bold_match.group(1).strip()
            field_value = bold_match.group(2).strip()
            metadata[field_name] = field_value
            current_section = field_name
            sections.setdefault(field_name, [])
            sub_bullets.setdefault(field_name, [])
            if field_name in repeatable_fields and field_value:
                sections[field_name].append(field_value)
            continue

        plain_match = PLAIN_FIELD_RE.match(line)
        if plain_match:
            field_name = plain_match.group(1).strip()
            field_value = plain_match.group(2).strip()
            metadata[field_name] = field_value
            current_section = field_name
            sections.setdefault(field_name, [])
            sub_bullets.setdefault(field_name, [])
            if field_name in repeatable_fields and field_value:
                sections[field_name].append(field_value)
            continue

        if current_section is not None and line.startswith("  - "):
            item = line[4:].strip()
            sections[current_section].append(item)
            sub_bullets[current_section].append(item)
            continue

        if line.strip():
            sections.setdefault("_body", []).append(line.strip())
            if not line.startswith("  "):
                current_section = None

    return metadata, sections, sub_bullets


def parse_dependencies(value: str) -> list[str]:
    lowered = value.strip().lower()
    if not lowered or lowered in {"nothing", "none"}:
        return []

    task_numbers: list[int] = []
    for range_match in TASK_RANGE_RE.finditer(value):
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        task_numbers.extend(range(start, end + 1))

    referenced_in_ranges = set(task_numbers)
    for task_match in TASK_REF_RE.finditer(value):
        task_number = int(task_match.group(1))
        if task_number not in referenced_in_ranges:
            task_numbers.append(task_number)

    return [f"TASK-{task_number:03d}" for task_number in unique(task_numbers)]


def infer_verification_commands(
    *,
    test_first: str,
    done_when: str,
    block: str,
    sections: dict[str, list[str]],
) -> list[str]:
    commands: list[str] = []

    for span in CODE_SPAN_RE.findall(test_first):
        if looks_like_command(span):
            commands.append(span)
        elif looks_like_test_file(span):
            commands.append(f"pytest {span} -x")

    if not commands:
        for span in CODE_SPAN_RE.findall(done_when):
            if looks_like_command(span):
                commands.append(span)

    if not commands:
        for item in sections.get("Create", []):
            for span in CODE_SPAN_RE.findall(item):
                if looks_like_test_file(span):
                    commands.append(f"pytest {span} -x")

    if not commands:
        for path in CODE_SPAN_RE.findall(block):
            if looks_like_test_file(path):
                commands.append(f"pytest {path} -x")

    if not commands and TEST_PASS_HINT_RE.search(done_when):
        commands.append("pytest -q")

    return unique(commands)


def looks_like_command(value: str) -> bool:
    return bool(COMMAND_HINT_RE.search(value)) and (" " in value or value in {"pytest", "bash", "sh"})


def looks_like_test_file(value: str) -> bool:
    normalized = value.strip()
    return normalized.endswith(".py") and (
        normalized.startswith("tests/")
        or "/test_" in normalized
        or normalized.split("/")[-1].startswith("test_")
    )


def extract_paths_from_section(items: list[str]) -> list[str]:
    paths: list[str] = []
    for item in items:
        code_spans = CODE_SPAN_RE.findall(item)
        if code_spans:
            first_span = code_spans[0]
            if looks_like_path(first_span):
                paths.append(first_span)
            continue
        for candidate in PATH_CANDIDATE_RE.findall(item):
            if looks_like_path(candidate):
                paths.append(candidate)
    return paths


def extract_paths_from_metadata(value: str) -> list[str]:
    if not value:
        return []
    paths: list[str] = []
    for span in CODE_SPAN_RE.findall(value):
        if looks_like_path(span):
            paths.append(span)
    for candidate in PATH_CANDIDATE_RE.findall(value):
        if looks_like_path(candidate):
            paths.append(candidate)
    return unique(paths)


def looks_like_path(value: str) -> bool:
    return bool(PATH_CANDIDATE_RE.fullmatch(value)) and not looks_like_command(value)


def build_description(block: str) -> str:
    lines = [line.rstrip() for line in block.splitlines()[1:]]
    cleaned = [line for line in lines if line.strip()]
    return "\n".join(cleaned).strip()


def render_plan_yaml(tasks: list[TaskDraft]) -> str:
    lines = ["tasks:"]
    for task in tasks:
        lines.extend(render_task_yaml(task))
    return "\n".join(lines) + "\n"


def render_task_yaml(task: TaskDraft) -> list[str]:
    lines = [
        f'  - id: {yaml_quote(task.id)}',
        f'    title: {yaml_quote(task.title)}',
        "    description: |",
    ]
    description_lines = task.description.splitlines() or [task.title]
    lines.extend(f"      {line}" for line in description_lines)
    if task.depends_on:
        lines.append("    depends_on:")
        lines.extend(f"      - {yaml_quote(item)}" for item in task.depends_on)
    else:
        lines.append("    depends_on: []")
    lines.append("    acceptance_criteria:")
    lines.extend(f"      - {yaml_quote(item)}" for item in task.acceptance_criteria)
    lines.append("    verification_commands:")
    lines.extend(f"      - {yaml_quote(item)}" for item in task.verification_commands)
    lines.append("    expected_files:")
    lines.extend(f"      - {yaml_quote(item)}" for item in task.expected_files)
    return lines


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def copy_spec(spec_path: Path, *, repo_root: Path, specs_dir: Path) -> None:
    destination_dir = specs_dir if specs_dir.is_absolute() else repo_root / specs_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spec_path, destination_dir / spec_path.name)


def unique(values: list[str] | list[int]) -> list[str] | list[int]:
    ordered: list[str] | list[int] = []
    seen: set[str] | set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


if __name__ == "__main__":
    raise SystemExit(main())
