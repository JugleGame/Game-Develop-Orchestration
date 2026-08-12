"""``build_ssl_context()`` 가 실제로 **검증하는** 컨텍스트를 만드는지 본다.

이 파일이 존재하는 이유는 하나다. 이 함수는 한동안 ``ssl.CERT_NONE`` 이었고,
그 상태는 오류를 내지 않는다 — 연결은 잘 되고 로그도 깨끗하다. 검증이 꺼졌다는
사실은 **아무 증상 없이** 조용히 유지되므로, 사람이 눈으로 발견하기를 기대할 수
없다. 그래서 기계가 본다.

네트워크를 타지 않는다. 여기서 재는 것은 컨텍스트의 설정값뿐이고, 실제 Neon
실제 핸드셰이크 재검증은 ``docs/backlog.md``의 Research 항목으로 추적한다.
"""

import ssl

import certifi

from strategic.research_repo import build_ssl_context

NEON_DSN = "postgresql://user:pw@ep-test-pooler.us-east-1.aws.neon.tech/db"


class TestBuildSslContext:
    def test_neon_dsn_gets_a_verifying_context(self) -> None:
        context = build_ssl_context(NEON_DSN)

        assert context is not None
        # 이 두 줄이 이 파일의 전부다. 하나라도 뒤집히면 외부망 연결이
        # 중간자 공격에 열린다.
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_certificates_come_from_the_certifi_bundle(self) -> None:
        """OS 저장소가 아니라 번들이어야 경로 문제와 무관해진다.

        certifi 번들의 인증서 수와 맞춰 보는 것으로 출처를 확인한다 — OS 저장소는
        머신마다 수가 다르고 certifi 와 일치하는 일이 사실상 없다.
        """

        context = build_ssl_context(NEON_DSN)
        assert context is not None

        expected = ssl.create_default_context(cafile=certifi.where())
        assert len(context.get_ca_certs()) == len(expected.get_ca_certs())

    def test_non_neon_dsn_is_left_alone(self) -> None:
        """로컬 Postgres 는 TLS 를 안 쓴다 — 여기에 컨텍스트를 물리면 연결이 깨진다."""

        assert build_ssl_context("postgresql://user:pw@localhost:5432/db") is None
