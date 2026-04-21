import os
import anthropic


class ClaudeClient:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = "claude-sonnet-4-20250514"

    def answer_spec_question(self, question: str, spec_context: str) -> str:
        """Answer an engineer's question grounded strictly in the spec content."""
        system = """You are SpecBot, an assistant for a software development team.
Your job is to answer questions about feature specifications accurately and concisely.

Rules:
- Answer ONLY from the spec content provided. Do not invent or assume anything not in the spec.
- If the spec doesn't cover the question, say so clearly: "The spec doesn't address this — you'll need to ask the PM directly."
- Be direct and precise. Engineers need unambiguous answers.
- Use bullet points or numbered lists where they aid clarity.
- Keep answers under 400 words unless the question genuinely requires more detail.
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

    def draft_proposal(self, transcript: str) -> str:
        """Turn a brainstorm transcript into a structured feature proposal."""
        system = """You are SpecBot, helping a product team turn brainstorm notes into structured feature proposals.

Your output must follow this exact structure:

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

    def revise_proposal(self, original_proposal: str, revision_notes: str) -> str:
        """Revise a proposal based on feedback."""
        system = """You are SpecBot. You have drafted a feature proposal and the team has given you revision notes.
Apply the changes faithfully, keeping the same structure. Return the complete revised proposal."""

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
