.PHONY: surface setup test demo

SKILL_DIR := skills/capital-markets-analyst

surface:
	@echo "FSI plugin skills:"
	@echo "  - fsi-capital-markets-analyst ($(SKILL_DIR))"

setup:
	$(MAKE) -C $(SKILL_DIR) setup

test:
	$(MAKE) -C $(SKILL_DIR) test

demo:
	$(MAKE) -C $(SKILL_DIR) demo
