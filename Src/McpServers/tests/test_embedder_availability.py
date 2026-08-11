"""``SentenceTransformerEmbedder.available()`` 이 무거운 import 를 하지 않는지 본다.

이 파일이 존재하는 이유는 하나다. 예전 구현은 존재 여부를 확인하려고
``import sentence_transformers`` 를 했고, 그 한 줄이 torch 를 통째로 끌고 와
**55.2초**가 걸렸다(2026-08-06 실측). 서버 lifespan 이 시작하자마자 이걸
부르므로 MCP stdio 핸드셰이크(30초 제한)가 통째로 터졌고, strategic 서버만
도구 목록에서 사라졌다.

이 결함은 **sentence-transformers 를 설치해야만 드러난다** — 없으면
ImportError 가 즉시 나서 빠르다. 즉 "임베딩을 켜면 경로 B 가 죽는다"는
형태라, 임베딩을 안 쓰는 사람에게는 영원히 안 보인다. 그래서 기계가 본다.

네트워크도 모델 파일도 타지 않는다. 재는 것은 "import 했는가" 하나뿐이다.
"""

import sys

from strategic.research_repo import SentenceTransformerEmbedder


class TestAvailableStaysCheap:
    def test_available_does_not_import_sentence_transformers(self) -> None:
        """이 한 줄이 이 파일의 전부다. 뒤집히면 핸드셰이크가 다시 터진다."""

        # 다른 테스트가 먼저 끌어다 놨으면 이 검사는 의미가 없다.
        if "sentence_transformers" in sys.modules:
            del sys.modules["sentence_transformers"]

        SentenceTransformerEmbedder.available()

        assert "sentence_transformers" not in sys.modules

    def test_available_reports_a_bool(self) -> None:
        """설치 여부와 무관하게 답은 나와야 한다 — 예외로 새면 서버가 못 뜬다."""

        assert isinstance(SentenceTransformerEmbedder.available(), bool)

    def test_model_load_is_still_deferred(self) -> None:
        """생성자도 모델을 안 만든다. 만들면 available() 을 고친 의미가 없다."""

        embedder = SentenceTransformerEmbedder()

        assert embedder._model is None
