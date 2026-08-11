# 12. PixelLab 에셋 생성 연동 — 구현 계획

**작성일**: 2026-08-02
**전제**: 이 문서는 별도 세션에서 "PixelLab 연동을 시작한다"는 작업을 이어받기 위한
핸드오프 문서다. 지금까지는 LoRA 자체 학습을 버리고 외부 에셋 생성기로
갈아탈지 조사만 했고, 코드는 `assetId` 충돌 수정 하나만 들어갔다(§2). PixelLab
API를 실제로 부르는 코드는 **아직 한 줄도 없다** — 이 문서가 그 착수 지점이다.

이 문서는 결론(무엇을 결정했다)과 미확인 사실(무엇을 아직 확인 안 했다)을
분리해서 적는다. 후자를 결론인 것처럼 코드에 박으면 안 된다 — 확인은 §3이
첫 단계다.

> **정정 (2026-08-03)**: §6 의 폴백 체인(PixelLab → LoRA → 절차적)은 **폐기됐다.**
> LoRA 추론과 절차적 렌더러를 코드에서 전부 걷어냈고(`b05a1ea`), 지금 생성 경로는
> PixelLab 하나다 — 키가 없거나 호출이 실패하면 §03 코드 3000 으로 실패한다.
> LoRA 설계 문서(09·10)도 지웠다(전문은 git 이력에 있다). 아래에서 LoRA·절차적
> 렌더러를 전제로 쓴 문장은 **그때의 계획**으로 읽는다.

---

## 0. 왜 PixelLab인가 (이전 대화 요약, 재검증 안 됨)

- 자체 LoRA 학습은 `character`/`icon` 카테고리는 품질 게이트를 통과했지만
  `tile`/`prop`은 9-epoch까지 실패했다(흐릿함/사진 같은 출력, CLIP 정합도
  0.22~0.247 vs 기준 0.25) — 실측치, `00_미완료_작업_목록.md`의
  "LoRA 에셋 모델" 절 참고.
- 이 머신 GPU는 `nvidia-smi` 실측으로 RTX 3060 Laptop, VRAM 6144MiB — SDXL급
  로컬 추론은 배경 제거 모델(BiRefNet)과 동시에 돌리기엔 여유가 빠듯하다.
- 팀 예산 월 $20 기준으로 PixelLab/RetroDiffusion/RunPod+HF/Unity AI
  Generator 4곳을 비교했고, PixelLab을 1순위로 꼽은 이유는 두 가지였다:
  (1) 픽셀아트 게임 에셋 전용 API라 크기별 과금이 $0.002~0.185/장으로 세분화돼
  있고, (2) "프롬프트 강화(prompt enhancement)" 엔드포인트가 있어 우리
  파이프라인이 지금 그대로 넘기는 다듬어지지 않은 한국어 프롬프트 문제를
  일부 흡수할 가능성이 있다(§3에서 실측 필요, 아직 검증 안 됨) — **2026-08-02
  공식 API 문서 확인 결과 이 가정은 틀렸다. 프롬프트 강화 엔드포인트는 PixelLab
  공식 스키마에 존재하지 않는다(§3 참고).**
- **이 근거는 이 세션의 대화 요약이지, 이 저장소 어떤 문서에도 아직 적혀 있지
  않다.** §11에서 `00_미완료_작업_목록.md`에 옮겨 적는 것을 제안한다.

---

## 1. 결정된 것 vs 아직 안 정해진 것

| 항목 | 상태 |
|---|---|
| LoRA 자체 학습을 신규 카테고리(tile/prop 등)에 계속 쓸지 | **결정**: 안 쓴다. 대신 외부 API |
| 어느 외부 API인지 | **결정**: PixelLab (사용자 확정, 2026-08-02) |
| PixelLab의 정확한 REST 스키마(엔드포인트 경로/인증 헤더/요청 필드) | **확인됨** (2026-08-02, §3) |
| 기존에 등록된 LoRA 모델(character/icon, `asset/lora_models/` 커밋됨)을 지울지 | **미정** — §6에서 폴백 체인에 남겨두는 것을 기본안으로 제안 |
| 청사진(blueprint)/기획AI 스키마 변경 | **결정**: 안 한다. `assetsNeeded`는 지금 배열 구조 그대로 쓴다(지난 세션 결론) |
| 한국어 프롬프트를 번역할지, PixelLab 프롬프트 강화에 맡길지 | **결정**: 번역 계층 필요 — PixelLab에 프롬프트 강화 엔드포인트 자체가 없음(§3 확인) |
| PixelLab 크레딧 비용을 어떻게 집계할지 | **미정** — §7, 확인된 갭 |

---

## 2. 지금까지 들어간 코드 변경 (같은 작업의 일부, 이미 완료)

- [`Src/McpServers/asset/server.py`](../../Src/McpServers/asset/server.py) —
  `_generate()`의 `asset_id`가 `{gameId}__{featureId}__{kind}`라 한 feature 안에
  같은 kind 에셋이 둘 이상이면(`assetsNeeded`에 "무기 아이콘"과 "방어구 아이콘"이
  둘 다 있는 경우 등) 파일과 매니페스트가 덮어써지던 결함을 고쳤다. 프롬프트
  SHA256 해시 8자를 `asset_id`와 파일명에 더했다(`_asset_path`도 시그니처
  변경). `00_미완료_작업_목록.md`의 "asset 서버 두 가지 결함" 중 2번이 이걸로
  해결됐다 — 1번(팔레트가 `artStyle`의 색상 키워드를 안 읽는 문제)은 아직 남아
  있고 이 작업과 무관하다.
- [`.claude/skills/game-planning/SKILL.md`](../../.claude/skills/game-planning/SKILL.md) —
  "시각적으로 구분되는 에셋은 배열 항목 하나씩 분리하라"는 한 줄 가이드 추가.
  청사진 스키마 자체는 안 건드렸다(§0 마지막 항목).

---

## 3. 착수 전 필수 확인 — 결과 (2026-08-02, `https://api.pixellab.ai/v1/openapi.json` 원문 확인)

**⚠️ v2로 갱신됨 (같은 날 2026-08-02, 뒤늦게 발견)** — 아래 v1 스키마로 클라이언트를
1차 구현한 뒤, `pixellab.ai/pixellab-api`(마케팅/가격 페이지)가 v1이 아니라
**v2 경로만 광고**하고 있는 걸 발견했다. `api.pixellab.ai/v2/openapi.json`
원문을 다시 확인한 결과:

- 베이스 URL이 `/v1`에서 **`/v2`로 바뀌었다.**
- pixflux 엔드포인트가 `/generate-image-pixflux` → **`/create-image-pixflux`**로
  개명됐다(동사가 `generate-`에서 `create-`로). 다른 엔드포인트도 같은 패턴일
  가능성이 높다(`/create-image-bitforge` 등으로 추정 — **미확인, 실제로 쓸 때
  다시 검증할 것**).
- 요청/응답 필드 모양은 그대로다(`description`/`image_size`/`no_background`/
  `seed`, 응답 `{image:{base64}, usage:{type,usd}}`) — **파싱 로직은 안 바뀐다.**
- 최소 이미지 크기가 16px → **32px**로 올라갔다. 우리 `style.pixel_grid`(32/48)는
  여전히 범위 안이라 영향 없음.
- v1이 아직 살아있는지는 실제 호출 없이 확인 불가(추측 금지) — 다만 PixelLab이
  지금 공개적으로 광고하는 유일한 버전이 v2이므로, 구현은 v2 기준으로 맞췄다
  (`asset/pixellab_client.py`, 2026-08-02 수정 반영).

아래 §3-1 표는 **v1 조사 당시 원문 그대로 보존**한다(무엇을 어떻게 확인했는지
감사 가능해야 하므로) — 실제 구현은 위 v2 정정을 따른다.

### 3-1. 스키마/인증/베이스 URL — v1 조사 시점 기록 (베이스 URL만 위 정정 참고)

- **베이스 URL**: ~~`https://api.pixellab.ai/v1`~~ → `https://api.pixellab.ai/v2`
- **인증**: `Authorization: Bearer <PIXELLAB_API_KEY>` (HTTP Bearer, v1/v2 동일)
- **엔드포인트 전체 목록 (v1 기준, path 접두사만 `/generate-` → `/create-`로
  바뀌었을 가능성 — 개별 재확인 전까지 참고용)**:
  | Path (v1) | Method | 용도 |
  |---|---|---|
  | `/generate-image-pixflux` (v2: `/create-image-pixflux`, 확인됨) | POST | 텍스트 → 픽셀아트 (32~400px, init/palette 지원) |
  | `/generate-image-bitforge` | POST | 스타일 참조 이미지 기반 생성 (최대 200×200) |
  | `/animate-with-skeleton` | POST | 스켈레톤 포즈 3프레임 애니메이션 |
  | `/animate-with-text` | POST | 텍스트 기반 애니메이션 (64×64 고정) |
  | `/rotate` | POST | 8방향 회전 (16/32/64/128px) |
  | `/inpaint` | POST | 인페인팅 (최대 200×200) |
  | `/estimate-skeleton` | POST | 스켈레톤 추정 (16~256px) |
  | `/balance` | GET | 계정 크레딧 잔액 조회 |

  타일셋/아이소메트릭 맵 생성 전용 엔드포인트는 v1 목록에 없었지만 — v2엔
  **있다.** `create-tileset` / `create-tileset-sidescroller` /
  `create-isometric-tile` / `map-objects`(가격 페이지 확인, 2026-08-02) — §0의
  "카테고리" 조사가 v1 기준이라 부정확했다. 지금 구현은 여전히 이 전용
  엔드포인트를 쓰지 않는다(pixflux로 대체) — 타일 품질이 아쉬우면 이쪽을
  검토할 것.

- **`CreateImagePixfluxRequest`(v2 이름) 필드**: `description`(str, 필수),
  `image_size`(`{width, height}`, 필수, **32**~400px, v2), `no_background`
  (bool), `seed`(int, 선택), `init_image`/`color_image`(선택, `Base64Image`).
  v1에 있던 `text_guidance_scale`/`outline`/`shading`/`detail`/`view`/
  `direction`/`isometric`/`init_image_strength`/`negative_description`도
  v2에 그대로 있다(2026-08-02 실제 요청으로 재확인, `v2/openapi.json` 원문 대조).

  **정정(2026-08-02, 실제 호출로 발견)**: 이 절이 처음 작성됐을 때 "`forced_palette`
  필드명(v2), 배열 형태"라고 적었던 것은 **틀렸다** — 실제 스키마엔
  `forced_palette`라는 필드 자체가 없다. 그 이름은 `color_image` 필드의
  **설명 문구**("Forced color palette")에서 나온 것이지 필드명이 아니었다.
  실제 구현으로 `{"forced_palette": [...]}`를 보냈더니 `422
  {"detail":[{"type":"extra_forbidden","loc":["body","forced_palette"]}]}`로
  거부당했다. 진짜 필드는 `color_image`: `Base64Image`
  (`{type:"base64", base64:<str>, format:"png"}`) — **팔레트를 이미지로
  인코딩해서 보내야 한다**(색상마다 1픽셀인 가로 스와치). `asset/pixellab_client.py`의
  `_palette_swatch_b64()`가 이걸 한다. 실제 API 호출(200 OK)로 검증됨 — §10-7 참고.
- **`CreateImagePixfluxResponse`**: `{ image: Base64Image, usage: Usage }`,
  v1과 동일 — `Usage`는 `{ type: "usd"|"generations", usd?: number,
  generations?: number }`, §7 비용 집계가 읽는 필드.
- 나머지 엔드포인트(bitforge 등)의 v2 정확한 필드는 착수 시점(실제로 그
  엔드포인트를 쓰게 될 때) 같은 방식으로 `v2/openapi.json`을 다시 확인한다
  — 지금은 pixflux만 v2로 검증·구현했다(우리가 실제로 쓰는 유일한 엔드포인트).

### 3-2. dry-run — 없음, 확인됨

RetroDiffusion류 `check_cost: true` 같은 기능이 **PixelLab에는 없다**.
`/balance`는 현재 잔액만 보여줄 뿐 생성 전 비용을 미리 계산해주지 않는다.
소액 실측(§10-7) 전에 비용을 미리 알 방법이 없다는 뜻 — 첫 실측은 가장 작은
`image_size`(32×32, 표에서 $0.002~0.029/장)로 시작한다.

### 3-3. 프롬프트 강화 엔드포인트 — 존재하지 않음, §0/§5 가정 정정

`openapi.json` 전체에 prompt enhancement/번역 관련 엔드포인트가 **없다**.
지난 세션이 근거 없이 가정한 기능이었다. 즉:

- 한국어 `assetsNeeded` 원문을 PixelLab에 그대로 넘겨도 강화해줄 장치가
  없다 — `description` 필드에 그대로 들어가면 모델이 한국어를 얼마나
  이해하는지는 별도 실측이 필요하지만, 강화 API에 위임하는 경로 자체가
  없으므로 **번역 계층은 선택이 아니라 필수**다(§5, §1 갱신).

### 3-4. 구독 티어 — 불필요, 확인됨 (2026-08-02, `pixellab.ai` 메인 페이지 실측)

Free 요금제에 이미 "API Access"와 "AI Agent Toolkit(MCP)"이 포함되어 있다
(pixellab.ai 메인 페이지, Subscription Tiers 섹션 원문 확인). Apprentice
($12/월)·Artisan($24/월)·Architect($50/월) 3단계는 **웹앱/Aseprite
플러그인 쪽** 월간 이미지 한도·최대 해상도(200→320→512px)·동시작업
수를 올려주는 구독이지, API 호출 자체의 전제조건이 아니다. API는
구독과 별개로 **건당 USD 크레딧 과금**이다(§3-1의 가격표, 요청 시점 결제).

구독이 실질적으로 필요해지는 유일한 지점은 "Map generation"(타일셋류,
§3-1에서 v2에 있다고 확인한 `create-tileset` 등) — Free엔 없고 Tier
1부터 있다. 지금 구현은 타일도 pixflux로 처리해 이 게이트에 안 걸린다.

**결론**: 구독 없이 [pixellab.ai/account](https://www.pixellab.ai/account)에서
API 토큰 발급 + 크레딧 충전만 하면 된다. 월 $20 예산(§0) 대비 실제 사용량
(32~48px 스프라이트, 장당 $0.002~0.03)은 여유가 크다.

---

## 4. 계정·시크릿

- `.env`에 `PIXELLAB_API_KEY` 추가, `common/env.py`가 이미 적용하는
  `load_repo_env()` 패턴을 그대로 탄다(`RESEARCH_DSN`/`LORA_PROJECT_PATH`와
  동일 취급 — 셸에 export된 값이 항상 이긴다, 빈 값은 미설정 취급).
  `.env.example`에도 플레이스홀더를 추가한다.
- `.mcp.json`에는 절대 안 적는다 — 그 파일은 커밋된다(CLAUDE.md 기존 규칙).
- API 키가 없을 때: 지금 LoRA가 없을 때 절차적 렌더러로 조용히 폴백하는 것과
  같은 패턴으로, PixelLab 키 미설정 시 PixelLab 분기를 건너뛰고 다음 분기로
  넘어가야 한다(§6) — 키 없는 사람의 CI/로컬 실행을 막으면 안 된다.

---

## 5. 규격화 어댑터 — 지난 세션 결론 반영

지난 조사에서 확인한 사실 셋:

- `assetsNeeded`는 여전히 자연어 한국어 문자열 배열이고(`assetgen.py`의
  `_requests_for()`가 이 배열을 그대로 순회해 프롬프트로 쓴다), 이 구조는
  바꾸지 않기로 했다.
- 크기 정보는 이미 존재한다 — `style.py`의 `ArtStyle.pixel_grid`가
  게임별로 32 또는 48로 고정되어 있고 (`_grid_for()`가 `artStyle` 문자열에
  "pixel"이 있는지로 결정), `server.py::_generate()`가 이미 이 값으로 절차적
  렌더링 캔버스를 정한다. PixelLab에 넘길 width/height도 **새 테이블을 만들
  필요 없이 이 값을 그대로 쓰면 된다** — UI 패널처럼 정사각형이 아닌 게 필요한
  kind만 예외 처리하면 충분하다.
- kind 추론도 이미 있다 — `render.classify(prompt)`가 `_generate()` 호출
  시점에 이미 kind를 정하고(`_generate_image()`에 인자로 넘어옴), PixelLab
  분기도 이 kind를 그대로 재사용하면 된다. 다시 분류할 필요 없다.

즉 "규격화 계층"의 실제 크기는 작다: kind는 이미 있고, size는
`style.pixel_grid`를 재사용하면 되고, 남는 건 (a) 한국어 → 영어 번역
(§3-3 확인 결과 필수 — PixelLab에 강화/번역 엔드포인트가 없다) 뿐이다.
새 스키마나 새 필드를 기획AI 쪽에 추가할 필요는 없다.

번역 계층 자체는 이 문서 범위 밖의 새 결정이 필요하다 — 후보는 (a) 별도
번역 API(예: 기존에 이미 쓰는 LLM 호출 경로가 있다면 재사용) 또는 (b) 고정
용어집(에셋 kind별 한→영 매핑, `assetsNeeded`가 "무기 아이콘 스프라이트"처럼
정형화된 패턴이 많다면 사전 매핑으로 충분할 수 있음). 어느 쪽이든 §10-1
체크리스트가 아니라 §10-3(클라이언트 모듈 작성) 직전에 결정한다.

---

## 6. 통합 지점 — `_generate_image()`

[`Src/McpServers/asset/server.py:174`](../../Src/McpServers/asset/server.py)의
`_generate_image()`가 지금 LoRA → 절차적 폴백 순으로 분기한다(§03 계약의
`generate_2d_sprite`/`generate_ui_asset`/`generate_3d_placeholder` 세 툴이 전부
이 함수를 거친다 — 여기 하나만 고치면 셋 다 적용됨).

기본안(다른 방식을 원하면 이 세션에서 재논의):

```
PixelLab 키 설정됨 + 호출 성공  → PixelLab 결과 사용
PixelLab 키 없음 / 호출 실패    → 기존 LoRA 분기(등록된 카테고리만) 시도
LoRA도 없음 / 실패              → render.render() 절차적 생성 (기존 그대로)
```

- 기존 LoRA 코드(`lora_inference.py`, `model_registry.py`)는 **지우지 않고
  그대로 둔다** — `character`/`icon`은 이미 실측으로 검증된 모델이 저장소에
  커밋돼 있고(`asset/lora_models/`), PixelLab 호출이 실패하거나 키가 없는
  환경에서 여전히 절차적 렌더러보다 나은 대안으로 쓸 수 있다. LoRA를 완전히
  걷어낼지는 PixelLab이 실제로 안정화된 뒤 별도로 결정한다(§1의 미정 항목).
- 반환 타입은 지금처럼 `(Image.Image, dict[str, str])` 유지. PixelLab 분기의
  provenance record는 `_lora_provenance()`/`render.provenance()`와 같은
  모양으로 만든다 — `method: "pixellab"`, 어떤 엔드포인트/모델을 썼는지,
  프롬프트 해시, 크레딧 비용(§7)을 담는다.
- **팔레트 고정(2026-08-02, 실측 후 추가)**: PixelLab은 호출 사이에 상태가
  없어서, 같은 게임의 두 에셋이 각자 다른 색을 골라도 막을 방법이 없었다
  (실측: 잠긴 보라색 캐릭터 팔레트의 게임에서 캐릭터는 보라색으로 나왔는데
  아이콘은 무관하게 은색/금색으로 나옴). `_generate_image()`가 이제
  `_pixellab_palette(style, kind, prompt)`로 `style.character_ramp()` /
  `style.material_ramp()` / UI 역할 색상(둘 다 절차적 렌더러가 이미 쓰는
  것)에서 팔레트를 뽑아 `color_image`로 실어 보낸다. 색상 수는 kind별로
  실측으로 정했다 — `character`는 램프(4) + `character_secondary` + 흰/검정
  = 7색(4색만 주면 실루엣이 뭉개짐, 눈·이빨·림라이트에 흑백이 필요), `tile`/
  `prop`은 램프(4) + `outline_for()` = 5색, `ui_panel`/`ui_button`/`icon`은
  UI 역할 4색 — 전부 그대로도 품질이 괜찮았다(실측 이미지 비교, §10-7).
- **생성은 파이프라인을 절대 막지 않는다**(server.py 모듈 docstring, 기존
  원칙) — PixelLab 호출의 네트워크 에러/타임아웃/쿼터 초과는 전부 예외를
  잡아 다음 분기로 넘어가야 한다. 사용자에게 에러를 그대로 띄우는 대신
  로그로 남기고 폴백한다(`LoraInferenceUnavailable`과 같은 패턴 — 이 예외를
  재사용하지 말고 `PixelLabUnavailable` 같은 별도 예외를 만든다, LoRA와
  PixelLab은 서로 다른 실패 사유를 가진다).

---

## 7. 비용 집계 — 확인된 갭

`Src/McpServers/common/usage.py::usage_of()`와
`Src/DeveloperAI/app/utils/usage.py::UsageTotals`는 **Anthropic 토큰 단가
전용**이다(`input_tokens`/`output_tokens`/캐시 토큰, `_PRICES_PER_MTOK`가
Claude 모델명으로만 키를 잡는다). PixelLab은 토큰이 아니라 이미지 장당
크레딧/달러로 과금되므로, 지금 구조에 `usage` 필드를 그대로 실어도
**`estimate_cost_usd()`가 모델명을 못 찾아 `unpriced_models`로 빠지고 비용은
0으로 잡힌다** — CLAUDE.md가 경계하는 "지출을 통째로 숨기는" 상황을 그대로
재현한다.

이 세션에서 결정할 것 (둘 중 하나, 또는 더 나은 제3안):

- (a) `generate_2d_sprite` 등의 반환값에 `costUsd` 같은 새 필드를 추가하고,
  `app/utils/usage.py::record()`가 `usage`뿐 아니라 이 필드도 읽어
  `UsageTotals.cost_usd`에 더하도록 확장한다(토큰 카운트와는 별도 경로).
- (b) 에셋 생성 비용은 아예 별도 필드/별도 테이블로 관리한다(토큰 비용과
  섞지 않음). 대시보드에서 "LLM 비용"과 "에셋 생성 비용"을 나눠 보여줘야 한다면
  이쪽이 맞을 수 있다.

월 $20 예산이 전제이므로 이 부분을 비워두고 넘어가면 안 된다 — 실제로 얼마
썼는지 아무 데도 안 남는다.

**결정 (2026-08-03): 제3안 — 달러가 아니라 이미지 장 수를 센다.**

실측으로 전제 하나가 틀린 것이 드러났다. PixelLab v2 응답은 달러를 아예 주지
않는다 — `{"type": "generations", "generations": 1.0}` 뿐이다. 계정도 Tier 1
구독제(월 $13.2 / 월 2000장)라, 호출 하나에 달러를 매기려면 월 요금을 한도로
나눈 **배분 단가**를 쓰는 수밖에 없는데 그것은 "이 잡이 쓴 돈"이 아니다 —
구독료는 이미 나간 돈이고, 한도를 넘기기 전까지 이미지 한 장의 한계비용은
0 이다.

그래서 (a)의 배선은 하되 단위를 바꿨다. 에셋 도구는 `imagesGenerated` 를
싣고, `app/utils/usage.py::record()` 가 이를 `UsageTotals.images_generated`
에 누적한다. `cost_usd` 에는 더하지 않는다 — 토큰 지출과 이미지 소진은 다른
장부다. 잡 하나가 이번 달 2000장 중 몇 장을 태웠는지가 실제 관리 대상이고,
그 값은 `game_jobs.token_usage` 에 함께 적재된다.

---

## 8. §03 계약 — 바뀌면 안 되는 것

`generate_2d_sprite(featureId, prompt, gameId, artStyle)` 등 세 툴의 시그니처와
반환 형태(`assetPath`/`assetId`/`kind`/`gameId`/`status`/`styleSeed`/
`generatedBy`)는 그대로 유지한다. PixelLab 연동은 전부 `_generate_image()`
내부 구현 디테일이어야 하고, `verify_contract.py`가 통과해야 한다(수정 후
`cd Src/McpServers && python verify_contract.py` 실행 확인).

---

## 9. 테스트

- `Src/McpServers/tests/test_asset_server.py`에 PixelLab 분기 테스트 추가.
  실제 네트워크 호출은 하지 않는다 — `httpx`/`requests` mock(또는 이 코드가
  쓰는 HTTP 클라이언트의 표준 목킹 방식)으로 성공/실패 양쪽을 검증한다.
- 확인할 것: (1) 성공 시 반환 계약이 §8과 동일한지, (2) 키 미설정 시 조용히
  다음 분기로 넘어가는지, (3) API 에러 시 파이프라인이 안 멈추고 폴백하는지,
  (4) `costUsd`(§7 결정에 따른 필드)가 정확히 채워지는지.
- 기존 47개(현재 48개, `assetId` 수정 후 전부 통과 확인됨) 테스트가 여전히
  통과해야 한다 — LoRA/절차적 경로는 건드리지 않는 변경이므로 회귀가 있으면
  그 자체가 버그다.

---

## 10. 실행 순서 (체크리스트)

1. ~~§3의 세 가지를 실제로 확인한다~~ **완료 (2026-08-02)** — 스키마/인증/
   베이스 URL 확인됨, dry-run 없음 확인됨, 프롬프트 강화 엔드포인트 없음
   확인됨(번역 계층 필수로 결정). 상세는 §3.
2. ~~`PIXELLAB_API_KEY` 환경변수 배선(§4)~~ **완료** — `.env.example`에
   추가, `common/env.py`가 그대로 적용한다(기존 패턴이라 코드 변경 불필요).
3. ~~`asset/pixellab_client.py` 신규 모듈~~ **완료** —
   [`asset/pixellab_client.py`](../../Src/McpServers/asset/pixellab_client.py).
   `generate-image-pixflux` 하나만 구현(실제 호출 지점이 이거 하나라서).
   한국어 프롬프트는 §3-3 결론대로 번역 없이 그대로 넘긴다 — LoRA 경로가
   이미 그렇게 하고 있어 기존 동작과 일치.
4. ~~`_generate_image()`에 PixelLab 분기 추가(§6)~~ **완료** —
   [`asset/server.py`](../../Src/McpServers/asset/server.py)의
   `_generate_image()`. 순서: PixelLab(키 있음) → LoRA(등록됨) → 절차적.
   실패는 전부 예외를 잡아 다음 단계로 넘긴다.
5. ~~§7 비용 집계 결정하고 반영~~ **완료 (2026-08-03 단위 정정)** — 처음엔
   (a)안의 `costUsd` 를 얹었으나, 실측 결과 PixelLab 이 달러를 주지 않아
   그 필드는 항상 비어 있었다. §7 결정문대로 단위를 이미지 장 수로 바꿔
   `_generate()` 가 `imagesGenerated` 를 얹고(§03 계약 필드는 그대로, 추가
   필드라 `verify_contract.py` 가 무시함), 오케스트레이터
   `app/utils/usage.py::record()` 가 이를 `UsageTotals.images_generated`
   에 누적한다. 양쪽 다 반영됐다.
6. ~~테스트(§9) 작성, `pytest` + `verify_contract.py` 통과 확인~~ **완료** —
   [`tests/test_pixellab_client.py`](../../Src/McpServers/tests/test_pixellab_client.py)
   (HTTP 목킹, §9의 성공/실패 양쪽),
   [`tests/test_asset_pixellab_branch.py`](../../Src/McpServers/tests/test_asset_pixellab_branch.py)
   (라우팅 우선순위 + `imagesGenerated` 노출). 기존 스위트 포함 전체 통과(6건
   실패는 이 저장소에 없는 형제 저장소·기존 산출물 관련 사전 존재 결함,
   이 작업과 무관 — 확인 방법: `git stash`로 이 변경을 뺀 트리에서도
   동일하게 실패함을 재현). `verify_contract.py`는 asset 서버만 단독
   기동해 확인(다른 4대는 이 작업과 무관하게 이 세션에서 안 띄움) —
   `8 tools`, 3종 §03 계약 도구 전부 OK.
7. ~~실제 API 키로 소액 실측~~ **완료 (2026-08-02)** — `PIXELLAB_API_KEY`가
   `.env`에 이미 설정돼 있었다. 캐릭터·아이콘·소품 각 2회 이상 생성해
   `generatedBy: "pixellab"`로 실제 호출됨을 확인했고, 그 과정에서
   `forced_palette` 필드명이 틀렸다는 것을 발견해 §3-1·§6에 반영했다
   (진짜 필드는 `color_image`). 팔레트 고정 전/후 이미지를 직접 비교해
   품질을 확인했다 — 고정 전엔 같은 게임의 두 에셋이 색상 계열이 서로
   무관했고(보라색 캐릭터 vs 은색/금색 아이콘), 고정 후엔 게임의 잠긴
   팔레트 색조를 공유한다. 캐릭터 kind는 팔레트를 4색으로 좁히면 실루엣이
   뭉개지는 것도 이때 발견해 7색(흑백 포함)으로 조정했다(§6).
   한국어 프롬프트 품질(§3-3)은 이 세션에서 한국어로 직접 실측하지 않았다
   — 다음 세션에서 확인할 것.
8. 끝나면 `00_미완료_작업_목록.md`에서 이 작업 관련 항목을 정리(§11).

### 3-5. 프롬프팅 최적화 실측 로그 (2026-08-02, 사용자 검증 7라운드)

`warrior-loot`(커밋된 샘플)의 실제 `assetsNeeded` 문장을 기반으로, 별도 테스트
게임(`prompt-eval`, 작업 후 삭제)에서 사용자가 매 라운드 입력/출력/점수를 직접
확인하며 진행했다. 원문 그대로 (입력 → 결과 → 점수):

| # | 입력 | 결과 | 점수 |
|---|---|---|---|
| 1 | 한국어 원문 그대로 (`"검을 든 전사 캐릭터, 픽셀아트"`, suffix 없음) | 검은 원형 blob, 전사·검 요소 없음 | 1 (FAIL) |
| 2 | 영어 번역 + `"pixel art game asset"` suffix | 명확한 전사+검, 게임 팔레트 반영 | 4 |
| 3 | 구조화 파라미터(`outline`/`shading`/`detail`/`view`) 도입, 프롬프트에 무기 유지 | 갑옷은 또렷하나 검이 안 보임 | 2 |
| 4 | 무기 제거, 캐릭터 디자인만 서술 | 깔끔한 기사 실루엣, 무기 없음 | 5 |
| 5 | #4와 동일, 생성 해상도만 32→128 네이티브 | 관절·바이저 등 디테일 뚜렷이 개선 | 5 |
| 6 | 몬스터(적 계열), 같은 스타일·팔레트·파라미터 재사용 | 같은 색조 공유 + 실루엣은 명확히 구분 | 5 |
| 7 | 소품(나무 궤짝), #4~6과 동일 shading(`medium shading`) | 재질은 맞으나 입체감이 과해 2D 룩과 안 맞음 | 3 |

**확정된 결론 세 가지 (전부 코드에 반영, `asset/server.py`):**

1. **한국어 원문 그대로 넘기면 안 된다** — 1점은 우연이 아니라 재현 가능한
   실패다. 이 세션은 영어로 대체 프롬프트를 만들어 실측했을 뿐, 청사진의
   한국어 `assetsNeeded`를 실제로 번역하는 계층은 아직 없다 — §11 백로그
   항목으로 남긴다.
2. **스타일은 텍스트가 아니라 구조화 파라미터로 걸어야 한다.**
   `outline="single color black outline"` / `detail="medium detail"` /
   `shading`은 kind별로 다르게(캐릭터는 `"medium shading"`, 나머지는
   `"flat shading"` — #7이 그 이유), `view`는 게임당 하나로 고정
   (`ArtStyle.camera_view`, `style.py::_view_for()` — 팔레트와 같은 방식으로
   잠근다. 시점을 매 호출 다르게 주면 같은 게임 안에서 시점이 뒤섞인다).
3. **무기/도구는 캐릭터 프롬프트에서 뺀다** — 장비는 게임에서 별도
   오브젝트로 붙는 것이고, 캐릭터 베이스 스프라이트에 들고 있는 걸로
   구우면 실패율이 높다(#3). 자동으로 걷어내는 문자열 처리는 넣지 않았다
   (원문을 짐작해서 자르는 것은 이 저장소의 "추측 금지" 원칙과 충돌) —
   기획 단계에서 `assetsNeeded`를 캐릭터/장비로 분리해 적는 것이 맞다.
4. **생성 해상도를 128로 올리고 게임 그리드로 다운샘플한다** (#5) —
   `_PIXELLAB_GENERATION_SIZE = 128` 다음 `LANCZOS`로 `style.pixel_grid`까지
   줄이고 그다음 `NEAREST`로 다시 4배 — LoRA 경로가 이미 쓰던 것과 같은
   패턴(512px 생성 → 다운샘플).

---

## 11. 백로그 반영 제안

`00_미완료_작업_목록.md`에 다음 항목 추가를 제안한다(이 문서 작성만으로는
아직 반영 안 함 — 사용자 승인 후 반영):

```
## PixelLab 에셋 생성 연동 (신규, 2026-08-02)

**상태**: 조사 완료, 결정 완료(LoRA 자체 학습 대신 PixelLab API 채택),
구현 착수 전. 상세 계획은 `12_PixelLab_에셋생성_연동_구현계획.md`.

**다음에 할 일**: 그 문서 §10 체크리스트 1번(PixelLab API 실제 스키마
재확인)부터.
```
