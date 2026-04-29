import os
import re
import json
import anthropic


class ClaudeClient:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = "claude-sonnet-4-6"

    def draft_proposal(self, transcript: str, format_example: str = "") -> str:
        """
        Turn a brainstorm transcript into a structured feature proposal.
        If format_example is provided, Claude mirrors its structure exactly.
        """
        if format_example:
            system = f"""You are SpecBot, helping a product team turn brainstorm notes into structured feature proposals.

The team has a specific way of writing specs. Here is a real example of one of their specs — study its structure, headings, tone, and level of detail carefully:

--- EXAMPLE SPEC START ---
{format_example}
--- EXAMPLE SPEC END ---

Your job is to write a new proposal that matches this format exactly:
- Use the same headings in the same order
- Match the tone and writing style
- Match the level of detail in each section
- Do not add sections that aren't in the example
- Do not remove sections that are in the example

Rules:
- Extract and organise ideas from the transcript faithfully — don't invent requirements.
- If something was discussed but not resolved, flag it clearly as an open question.
- Be concise. Match the length and density of the example spec.
- Use plain language. No jargon unless it was used in the transcript."""

        else:
            system = """You are SpecBot, helping a product team turn brainstorm notes into structured feature proposals.

Your output must follow this structure:

# [Feature Name]

## Overview
One paragraph summarising what this feature does and why it exists.

## Problem Statement
What problem does this solve? Who experiences it?

## Proposed Solution
How will it work? Describe the approach at a high level.

## Key Requirements
- Bullet list of must-have requirements

## Out of Scope
- Bullet list of things explicitly NOT included in this version

## Open Questions
- List any unresolved decisions or things needing follow-up

## Success Criteria
How will we know this feature succeeded?

Rules:
- Extract and organise ideas from the transcript faithfully — don't invent requirements.
- If something was discussed but not resolved, put it in Open Questions.
- Be concise. A good proposal is one page, not five.
- Use plain language. No jargon unless it was used in the transcript."""

        prompt = f"""Here is the brainstorm transcript:
---
{transcript}
---

Write the feature proposal."""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=1200,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text

    def draft_section_edit(self, page_content: str, section_name: str, instruction: str) -> dict:
        """
        Rewrite a specific section of a spec page based on an instruction.
        section_name can be "auto" to let Claude choose the most relevant section.
        Returns {"section_heading": str, "revised_section": str, "summary": str}.
        """
        section_directive = (
            f'The section to edit is: "{section_name}"'
            if section_name.lower() != "auto"
            else "Choose the most relevant section to edit based on the instruction."
        )

        system = """You are SpecBot, helping a product team maintain their Confluence spec pages.
You will be given a spec page and an instruction to edit one section of it.

Your response MUST be a JSON object with exactly these fields:
{
  "section_heading": "The exact heading text of the section you edited",
  "revised_section": "The full revised content in markdown (heading line + body)",
  "summary": "One sentence describing what you changed and why"
}

Rules:
- Only modify the section specified. Do not alter other sections.
- Keep the section heading exactly as it appears in the original.
- Maintain the writing style and format of the surrounding document.
- Apply the instruction faithfully — do not add unrequested changes.
- revised_section must include the heading line (e.g. "## Requirements") followed by the new content.
- Respond with the JSON object only — no markdown code fences, no explanation."""

        prompt = f"""Spec page content:
---
{page_content}
---

{section_directive}

Instruction: {instruction}"""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=1200,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)

    def summarize_spec_changes(self, old_content: str, new_content: str, page_title: str, versions: list[dict]) -> str:
        """
        Produce a human-readable Slack-formatted changelog by comparing two snapshots
        of a spec page, enriched with the edit history for the period.
        """
        version_lines = []
        for v in versions:
            when = v["when"]
            when_str = when.strftime("%b %d %H:%M UTC") if hasattr(when, "strftime") else str(when)
            note = f' — "{v["message"]}"' if v.get("message") else ""
            version_lines.append(f"- {when_str}: edited by {v['by']}{note}")
        version_history = "\n".join(version_lines) if version_lines else "No edit messages recorded."

        system = """You are SpecBot, summarising what changed in a product spec over a given period.

You will be given two snapshots of the same spec page — the version before the period started and the current version — plus a list of who made edits and when.

Write a concise Slack-formatted changelog. Focus on:
- Which sections changed and what the meaningful difference is
- New requirements added, items removed, or decisions reversed
- Do NOT mention formatting-only changes, whitespace, or trivial typo fixes

Format as a bullet list, one bullet per meaningful change:
• *Section name*: what changed

If the content is identical or only trivially different, say so plainly.
Keep the total response under 800 characters."""

        prompt = f"""Spec page: {page_title}

Edit history for this period:
{version_history}

--- VERSION BEFORE THIS PERIOD ---
{old_content}

--- CURRENT VERSION ---
{new_content}

Summarise the meaningful changes."""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text

    def identify_section_changes(self, transcript: str, page_content: str) -> list[dict]:
        """
        Analyse a meeting transcript against an existing spec page.
        Returns list of {"section_heading", "revised_section", "summary"} for each
        section where the meeting produced a clear decision to update content.
        Returns [] if nothing actionable was discussed.
        """
        system = """You are SpecBot, helping a product team keep their spec pages up to date during live meetings.

You will be given:
1. The current spec page content
2. A live meeting transcript where the team may be discussing changes to the spec

Your job is to identify sections where the team reached a CLEAR DECISION to change something, then rewrite those sections.

Your response MUST be a JSON array. Each element represents one section that needs updating:
[
  {
    "section_heading": "Exact heading text as it appears in the spec",
    "revised_section": "Full revised section in markdown (heading line + body)",
    "summary": "One sentence: what changed and why"
  }
]

If no sections need updating, return an empty array: []

Rules:
- Only update sections where the team made a concrete decision or stated clear direction.
- Ignore passing mentions, open questions without answers, and hypothetical discussion.
- Keep each section heading exactly as it appears in the existing spec.
- Preserve the writing style and level of detail of the existing spec.
- revised_section must include the heading line (e.g. "## Key Requirements") followed by the updated body.
- Respond with the JSON array only — no explanation, no markdown code fences."""

        prompt = f"""Current spec page:
---
{page_content}
---

Meeting transcript so far:
---
{transcript}
---

Return the JSON array of sections to update based on clear decisions made in the meeting."""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return result if isinstance(result, list) else []

    def answer_general(self, question: str) -> str:
        """Answer with full Claude capability."""
        system = """You are SpecBot, an assistant for a software development team.
Answer questions helpfully and thoroughly using your full capability.
Be direct and concise. Use bullet points or numbered lists where they aid clarity.
If a question is about an internal team decision that only the team's own documentation could answer, say so clearly."""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": question}]
        )
        return message.content[0].text

    def answer_spec_question(self, question: str, spec_context: str) -> str:
        """Answer an engineer's question using the spec content as primary source."""
        system = """You are SpecBot, an assistant for a software development team.
Your job is to help engineers understand, navigate, and analyse feature specifications.

You can:
- Answer questions directly from the spec
- Summarise the whole spec or specific sections
- Highlight key requirements, decisions, and open questions
- Draw reasonable inferences when the spec clearly supports them
- Compare, contrast, or explain trade-offs described in the spec

Rules:
- Use the spec content as your primary source. Do not invent facts not supported by it.
- If the spec genuinely does not cover something, say so and suggest who to ask (e.g. the PM).
- Be direct and concise. Use bullet points or numbered lists where they aid clarity.
- Keep answers under 600 words unless the question genuinely requires more detail.
- Never pad your answer or repeat the question back."""

        prompt = f"""Spec content:
---
{spec_context}
---

Engineer's question: {question}"""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text

    def revise_proposal(self, original_proposal: str, revision_notes: str, format_example: str = "") -> str:
        """Revise a proposal based on feedback, maintaining the gold standard format."""
        format_instruction = ""
        if format_example:
            format_instruction = f"""
The team's spec format is shown in this example — maintain it throughout your revision:
--- EXAMPLE SPEC START ---
{format_example}
--- EXAMPLE SPEC END ---
"""
        system = f"""You are SpecBot. You have drafted a feature proposal and the team has given you revision notes.
Apply the changes faithfully.{format_instruction}Return the complete revised proposal."""

        prompt = f"""Original proposal:
---
{original_proposal}
---

Revision notes from the team:
{revision_notes}

Return the full revised proposal."""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=1200,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
