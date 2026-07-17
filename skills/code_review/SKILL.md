---
name: code-review
description: Python code review guidelines and checklist
---

# Code Review Skill

## Checklist

- [ ] Type hints: all public functions annotated
- [ ] Docstrings: all modules, classes, and public functions
- [ ] Error handling: exceptions properly caught, no bare `except:`
- [ ] Naming: snake_case for functions/variables, PascalCase for classes
- [ ] Imports: standard library first, third-party second, local last
- [ ] Line length: max 100 characters
- [ ] Tests: new functions have corresponding unit tests

## Process

1. Read the file to review
2. Check each item in the checklist above
3. Report findings with file path and line numbers
