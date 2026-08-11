---
name: spec-lint
description: 기획 산출물(청사진 + spec JSON)이 프로젝트의 발행 규칙을 지키는지 검사한다. 사용자가 "spec 검사해줘", "기획 검증해줘", "린트 돌려줘"라고 하거나, game-planning 스킬이 기획을 만든 직후에 반드시 실행한다. 합격 기준의 측정 가능성, 카드 인용 실재성, 의존 순환을 잡는다.
---

# spec 검사 — 통과하지 못한 기획은 넘기지 않는다

## 실행

```bash
python .claude/skills/spec-lint/scripts/lint_spec.py <기획.json>
```

리서치 카드 목록을 알고 있다면 그것만 인용 가능한 것으로 제한한다:

```bash
python .claude/skills/spec-lint/scripts/lint_spec.py plan.json --cards ELEM-001 GENRE-004 GAME-013
```

`--cards` 를 생략하면 청사진이 인용한 카드를 그대로 인정한다. 이 경우 "존재하지
않는 카드를 지어냈는가"는 잡지 못하고 **내부 일관성만** 본다. `research_idea` 로
받은 카드 목록이 있으면 반드시 `--cards` 로 넘긴다.

종료 코드: `0` 통과 / `1` 위반 있음 / `2` 입력 오류.

## 검사 항목

**spec 단위 (S2~S6)** — 원본 [strategic/specs.py::lint_spec](../../../Src/McpServers/strategic/specs.py)
을 그대로 import 해서 돌린다. 이 스크립트는 규칙을 복사하지 않는다.

| 코드 | 내용 |
|---|---|
| S2 | `specId` / `version` / `blueprint_version` / `refs` 필수 |
| S3 | `refs` 는 `ELEM\|GENRE\|GAME\|ARCH-###` 형식이고 **실재하는 카드**여야 함 |
| S4 | 목표 · 구현 범위 · 제외 범위 · 합격 기준 중 빈 것이 없어야 함 |
| S5 | 합격 기준에 금지어가 없고, 숫자 또는 관찰 키워드가 있어야 함 |
| S6 | 인용한 `ARCH` 카드의 지침이 실제로 실려 있어야 함 (아래) |

형식 목록(카드 ID 패턴·금지어·관찰 키워드)은 코드에 박혀 있지 않다 —
`strategic/spec_rules.json` 한 장이 원본이고 두 저장소가 그 파일을 공유한다.

**청사진 단위 (B1~B6)** — 이 스킬이 더하는 검사.

| 코드 | 내용 |
|---|---|
| B1 | spec 은 최소 3개 이상 |
| B2 | `synergyRationale` 최소 2장 |
| B3 | 반례가 있으면 `counterEvidenceNote` 는 비어야 하고, 없으면 정확히 `"반례 조사 부족"` |
| B4 | `dependencies` 는 실재하는 `specId` 만 가리킴 |
| B5 | 의존 순환 금지 |
| B6 | 청사진이 인용한 카드도 실재해야 함 |

## S6 — 아키텍처 카드 인용이 빈 인용이 아닌지

`refs` 에 `ARCH-###` 가 있으면, 그 카드의 구현 절차·안티패턴·검증 방법이 spec 의
`architecture` 에 실려 있어야 한다. 개발 AI 는 spec 하나만 받으므로, 인용만 있고
내용이 없으면 **그 인용은 개발 AI 에게 아무 지시도 하지 않는다.**

| 메시지 | 뜻과 고치는 법 |
|---|---|
| `인용했는데 아키텍처 지침이 실리지 않음` | 대개 5.5단계를 건너뛴 것이다. `attach_arch_guidance.py` 를 돌린다 |
| `refs 에 없는 카드의 지침이 실려 있음` | `refs` 를 고쳤는데 지침을 다시 붙이지 않았다. 스크립트를 다시 돌린다 |
| `절이 비어 있음` | 카드 쪽이 깨졌다. 그 카드에 `lint_card.py` 를 돌려 절 제목을 확인한다 |

`architecture` 를 **손으로 쓰지 않는다.** 카드 원문과 어긋나는 순간 S3(인용 실재성)는
통과하는데 내용은 다른 상태가 되고, 그건 어느 검사도 잡지 못한다. 붙이는 일은
[game-planning](../game-planning/SKILL.md) 5.5단계의 스크립트가 한다:

```bash
python .claude/skills/game-planning/scripts/attach_arch_guidance.py plan.json
```

## 실패했을 때

**지적된 항목만 고치고 다시 검사한다.** 규칙을 완화하거나 검사를 우회하지 않는다.

가장 흔한 실패는 S5 다. 합격 기준을 이렇게 고친다:

| ✗ 실패 | ✓ 통과 |
|---|---|
| `이동이 자연스럽다` | `입력 후 0.1초 안에 Rigidbody2D.velocity 가 바뀐다` |
| `적절한 수의 적이 나온다` | `한 청크에 적이 3~5개 스폰된다` |
| `좋은 성능` | `60프레임에서 프레임 드랍이 0건이다` |

금지어: `재미` `좋은` `좋아` `멋진` `자연스러` `적절` `재치`
`fun` `nice` `nicely` `good` `cool` `great` `awesome` `natural` `naturally`
`appropriate` `appropriately` `proper` `properly` `clever` `witty`

관찰 키워드(숫자가 없을 때 최소 하나 필요): `로그` `콘솔` `테스트` `씬` `파일`
`커밋` `초` `개` `프레임` `%` `log` `console` `test` `scene` `file` `commit`
`sec` `second` `seconds` `frame`

> 청사진이 영어로 나오면(2026-08-02, `12_PixelLab_에셋생성_연동_구현계획.md`
> §10-7 이후 결정) 위 영어 항목이 실제로 걸리는 쪽이다. 영어는 substring 이
> 아니라 단어 경계로 매칭한다(`specs.py::_word_present()`) — 그래서 `fun` 은
> `function` 에, `cool` 은 `cooldown` 에, `proper` 는 `property` 에 걸리지
> 않는다.
