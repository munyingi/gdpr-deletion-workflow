export PYTHONPATH := src

.PHONY: data pipeline demo app test clean

data:
	python -m gdpr generate

pipeline:
	python -m gdpr pipeline

demo: data pipeline
	python -m gdpr discover C00042
	python -m gdpr erase C00042 --received-on 2026-11-10 --restore-drill
	python -m gdpr audit --verify

app:
	streamlit run app/streamlit_app.py

test:
	python -m pytest -q

clean:
	rm -rf data dbt/target dbt/logs
