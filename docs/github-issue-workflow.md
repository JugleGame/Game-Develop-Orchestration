# GitHub Issue workflow

## 흐름

Issue는 작업 계약이고, Issue 브랜치는 `dev`에만 병합한다. `main`에는 검증된
`dev`를 통합하는 PR만 허용한다.

```text
Issue #123 생성
  -> 123-fix-issue-contract-check 브랜치 생성
  -> 변경 및 검증
  -> dev 대상 PR에 Refs #123 작성
  -> 자동 검사와 리뷰
  -> dev에 병합하고 Issue 수동 종료
  -> 준비된 변경 묶음을 dev -> main 통합 PR로 병합
```

GitHub의 closing keyword는 기본 브랜치 대상 PR에서만 동작한다. 따라서 `dev` 대상
PR에는 `Closes #123`이 아니라 `Refs #123`을 사용하며, 병합 후 Issue를 수동 종료한다.

## Issue 브랜치 규칙

1. 정상 개발은 열린 Issue에서 시작한다.
2. Objective, Scope, Out of Scope, Acceptance Criteria와 Test를 전부 읽는다.
3. 브랜치는 `<number>-<type>-<short-description>` 형식을 사용한다. `type`은
   `feat`, `fix`, `refactor`, `test`, `docs`, `chore` 중 하나다.
4. PR의 base는 `dev`여야 한다.
5. PR 본문에는 정확히 하나의 `Refs #<number>`를 작성한다.
6. 한 Issue는 보통 브랜치와 PR 하나로 처리한다.
7. 자동 테스트와 계약 검사, 리뷰가 통과한 뒤 병합하고 Issue를 수동 종료한다.

## 문서 오타 예외

Issue 없는 문서 오타는 `docs/<short-description>` 브랜치로 `dev`에 PR을 만든다.
PR 본문에는 다음 문구를 정확히 작성한다.

```text
No-Issue-Reason: Typo-only documentation change
```

변경은 `README.md`와 `docs/` 아래로 제한된다. 코드, 설정, 의존성 변경에는 Issue가
필요하다.

## dev 통합 PR

`main`에는 head가 `dev`인 통합 PR만 허용한다. 본문에는 다음 문구를 작성한다.

```text
Integration-Reason: dev-to-main integration
```

통합 PR은 Issue 번호 브랜치 규칙을 적용하지 않지만 전체 테스트와 계약 검사를
통과해야 한다. `dev` 이외의 브랜치에서 `main`으로 직접 PR을 만들지 않는다.

## 자동 검사

- `.github/ISSUE_TEMPLATE/work-item.yml`은 작업 Issue에 필요한 항목을 요구한다.
- `.github/pull_request_template.md`는 Issue 참조, 변경 이유, 테스트와 완료 조건을 묻는다.
- `Issue contract`는 base/head 조합, 브랜치 이름, 열린 Issue와 `Refs #N`을 검사한다.
- 문서 오타 예외는 허용 파일과 `No-Issue-Reason`을 검사한다.
- `dev -> main`은 정확한 `Integration-Reason`을 검사한다.
- `Tests`는 모든 PR과 `dev`, `main` push에서 Python 테스트와 MCP 계약 검사를 실행한다.

## GitHub 관리자 설정

저장소 파일만으로 병합을 막을 수 없으므로 관리자는 `dev`와 `main` 각각에 branch
ruleset을 설정해야 한다.

두 브랜치 공통:

1. **Settings > Rules > Rulesets > New branch ruleset**에서 대상 브랜치를 선택한다.
2. **Require a pull request before merging**를 활성화한다.
3. **Require status checks to pass**에서 다음 검사를 요구한다.
   - `Validate issue, branch, and PR`
   - `Python tests and contract check`
4. 승인을 최소 1개 요구한다.
5. force push를 차단한다.

`Issue contract` workflow가 Issue와 PR을 읽을 수 있도록 `issues: read`,
`pull-requests: read`, `contents: read` 권한을 허용한다. `main` ruleset은 workflow가
검사하는 `dev -> main` PR만 운영 정책상 승인한다.

`dev`는 장기 통합 브랜치이므로 삭제하지 않는다. `dev`에 병합된 Issue 브랜치만
병합 후 삭제한다.

## 참고

- 자동 검사는 Issue가 열려 있는지 확인한다. 별도의 `ready` label 규칙은 없다.
- PR 본문을 수정하면 검사가 다시 실행된다.
- `dev` 병합은 Issue를 자동 종료하지 않으므로 사람이 완료 조건을 확인하고 닫는다.
