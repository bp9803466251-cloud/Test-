# Makefile — team_registry workflow
# Usage: make <target>

.PHONY: test lint all collectors dump pipeline clean

# Run gluing tests
test:
	python test_gluing.py

# Check Python syntax
lint:
	python -m py_compile team_registry_final.py
	@echo "✅ Syntax OK"

# Pre-deploy: lint + test
all: lint test
	@echo "✅ Ready to deploy"

# Run all 4 collectors (adjust paths as needed)
collectors:
	python collectors/oddsapi_collector.py
	python collectors/bzzoiro_collector.py
	python collectors/sharpapi_collector.py
	python collectors/propline_collector.py
	@echo "✅ All collectors done"

# Run Match Dump to check for duplicates
dump:
	python match_dump.py
	@echo "✅ Match Dump done"

# Run Sports Pipeline
pipeline:
	python sports_pipeline.py
	@echo "✅ Pipeline done"

# Clean pyc files
clean:
	rm -f *.pyc __pycache__/ -r
