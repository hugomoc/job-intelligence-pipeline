from pathlib import Path


def test_title_classification_summary_wording() -> None:
    source = Path("streamlit_app.py").read_text()

    assert "Filtered by title:" not in source
    assert "Title-classified as filtered out:" in source


def main() -> None:
    test_title_classification_summary_wording()
    print("Streamlit label tests passed.")


if __name__ == "__main__":
    main()
