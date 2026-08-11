---
name: unity-codegen
description: spec 또는 기능 설명을 Unity 6 용 C# MonoBehaviour 스크립트로 생성하고 UnityMcpServer 를 통해 프로젝트에 투입한다. 사용자가 "이 spec 구현해줘", "C# 짜줘", "Unity 스크립트 만들어줘", "컴파일 오류 고쳐줘"라고 하거나, 기획 산출물을 실제 코드로 옮겨야 할 때 사용한다. UnityMcpServer 의 LLM 생성 단계를 API 키 없이 대체하는 경로다.
---

# Unity C# 생성 — 규칙은 명세가 정한다

이 스킬은 [Src/McpServers/unity/codegen.py](../../../Src/McpServers/unity/codegen.py)
의 `SYSTEM_PROMPT` 를 그대로 옮긴 것이다. `Doc/설계/04_Prompt_Specification.md`
의 "Unity AI" 절이 원본이다.

## ⚠️ 먼저: 배치로 생성한다

`claude -p` 호출 한 번마다 Claude Code 시스템 프롬프트 **약 25,000 토큰**이 실린다.
스크립트 8개를 8번 나눠 호출하면 오버헤드만 8배다.

> **spec 을 하나씩 처리하지 말고, 한 번에 전부 읽고 전부 생성한 뒤 한 번에 투입한다.**

## ⚠️ 그다음: 코드를 쓰기 전에 설계 패스를 먼저 부른다

**게임당 한 번**, 어떤 스크립트보다 먼저 이것을 부른다.

```
unity.design_architecture(
    gameId         = "<게임 id>",
    gameDesign     = <청사진 dict>,
    featurePrompts = [{feature_id, title, description}, ...],
    design         = <완성된 설계안>          # ← 이 경로에서는 항상 채운다
)
```

`design` 을 채우면 서버는 모델을 부르지 않고 **검증만** 한다 (키 불필요).
비우면 키가 없어 실패한다 — `create_script` 의 `contents` 와 같은 규칙이다.

**설계안이 정하는 것**: 파일 목록(`files[]`), 프리팹(`prefabs[]`), 씬 구성(`scene`).
그 결과가 이후 모든 호출의 인자가 된다.

| 설계안의 자리 | 그것을 받는 호출 |
|---|---|
| `files[].path` · `files[].className` | `create_script(plannedPath=…, plannedClass=…)` |
| `prefabs[]` 한 항목 | `create_prefab` |
| `scene` | `compose_scene` |

**설계안은 게임당 한 번이다.** 재시도할 때 다시 뽑지 않는다 — 회차마다 구조가
흔들리면 재현성이 무너진다. 이미 뽑은 것을 그대로 다시 넘긴다.

**서버가 거부하는 설계안** (넘긴 값도 생성한 값과 똑같이 검사된다):

- `className` 이 `^Spec\d+$` 같은 문서 번호 이름
- `path` 가 `Assets/Scripts/{Category}/{ClassName}.cs` 형식이 아님
- 파일명과 `className` 이 다름
- 어떤 파일도 구현하지 않는 `feature_id` 가 있음
- `dependsOn` 에 순환이 있거나 없는 파일을 가리킴
- **어떤 프리팹·씬 오브젝트에도 붙지 않는 `MonoBehaviour` 가 있음**

마지막 것이 이 규칙의 알맹이다. 붙을 자리가 없는 타입이라면 애초에
`MonoBehaviour` 가 아니어야 한다 — `kind` 를 `plain` 이나 `ScriptableObject` 로
바꾼다.

## 코드 규칙 (전부 강제)

- **Unity 6 (6000.x)** 대상. 입력 처리는 **새 Input System** 을 쓴다.
- **파일 하나에 public 클래스 하나.** 클래스명은 파일명과 **정확히** 일치해야 한다.
- 기능이 명백히 `ScriptableObject` 나 순수 클래스를 요구하지 않는 한 `MonoBehaviour`
  를 상속한다.
- 주어진 **네임스페이스**에 타입을 넣는다. 다른 것을 지어내지 않는다.
  (기본값 `Game.Gameplay` — `UNITY_SCRIPT_NAMESPACE` 로 변경됨)
- 인스펙터에 노출할 필드는 `public` 이 아니라 **`[SerializeField] private`** 로 쓴다.
- `GetComponent` / `Find` 로 얻은 참조는 **쓰기 전에 null 검사**한다.
- **주석은 전부 한글로 쓴다.** 클래스 선언 바로 위에 `// 기능: spec-003` 처럼
  feature id 를 적은 한 줄 주석을 단다. QA 가 런타임 오류를 원인 기능으로
  역추적할 수 있어야 한다.
- **`Debug.Log`/`Debug.LogWarning`/`Debug.LogError` 메시지도 한글로 쓴다.**
  무슨 일이 있었는지, 디버깅에 필요한 값이 무엇인지 담는다 — "설명 금지"
  규칙의 유일한 예외가 이 주석·로그 문자열이다. 식별자·키워드·API 이름은
  그대로 C# 이다.
- **2D 게임이다.** `Rigidbody2D` / `Collider2D` / `Vector2` 를 3D 대응물보다 우선한다.

## spec 에 `## 아키텍처 지침` 절이 있으면

그 절은 리서치 아키텍처 카드(`ARCH-###`)의 **원문**이다. 기획이 골라 붙인 것이고,
구현 방식에 대해서는 spec 본문보다 이쪽이 구체적이다.

- **`#### Unity 구현 절차`** — 번호 순서가 지시다. 그 순서대로 구조를 만든다.
- **`#### 안티패턴`** — 생성한 코드를 넘기기 전에 **이 목록으로 자기 점검한다.**
  하나라도 해당하면 고쳐서 다시 본다. 컴파일이 되는 것과 이 목록을 피한 것은
  다른 문제다.
- **`#### 검증 방법`** — QA 가 이 기준으로 본다. 여기 적힌 것이 관찰 가능하도록
  (예: 로그 한 줄) 코드에 남긴다.

**카드가 못 박은 수치나 범위를 늘리지 않는다.** 예를 들어 활성 범위 3x3 을
5x5 로 바꾸는 것은 최적화가 아니라 구조 변경이고, **사람 승인 사항이다.** 더 좋아
보여도 바꾸지 말고, 근거가 있으면 코드를 고치는 대신 그 사실을 보고한다.

카드를 직접 찾아 읽지 않는다. spec 에 실려 온 것이 전부이며, 그게 기획·개발·QA 가
같은 문장을 보게 하는 방법이다.

## 출력 규칙

- **C# 소스만** 출력한다. 산문·설명·마크다운 펜스 금지.
- 출력은 **그대로 컴파일되어야 한다.**

## 클래스명·경로 규칙 — 설계안이 정한다

**이름을 여기서 짓지 않는다.** `design_architecture` 가 이미 지었다. 그 값을
`plannedClass` / `plannedPath` 로 그대로 넘긴다.

- 클래스명은 **게임 개념**이다 — `ChunkLoader`, `PlayerController`, `LootTable`.
- 경로는 **역할별 폴더**를 갖는다 — `Assets/Scripts/{Category}/{ClassName}.cs`
  (`World` · `Player` · `Systems` · `UI` · `Data` 가 흔하다). 04 명세의
  **Folder Rule** 이 이것이다.
- 파일명과 public 타입 이름이 **정확히** 같아야 한다. Unity 는 MonoBehaviour 를
  파일명으로 찾으므로, 다르면 스크립트가 씬에 붙지 않는다.

> **예전 규칙은 폐기됐다.** `feature_id` 를 PascalCase 로 바꿔
> (`spec-001` → `Spec001`) 클래스 이름으로 쓰던 규칙이 여기 적혀 있었다.
> 그 규칙 때문에 `Assets/Scripts/Spec001.cs` ~ `Spec006.cs` 가 평면으로 나왔고,
> 지금은 `verify_project_layout.py` 의 L1 이 그 이름을 **FAIL 로 거부한다.**
> `codegen.py::_sanitize_class_name` 은 설계안이 없을 때만 도는 최후 수단이다.

## 예시

```csharp
using UnityEngine;

namespace Game.Gameplay
{
    // 기능: spec-003
    public sealed class PlayerController : MonoBehaviour
    {
        [SerializeField] private float moveSpeed = 5f;

        private Rigidbody2D _body;

        private void Awake()
        {
            _body = GetComponent<Rigidbody2D>();
            if (_body == null)
            {
                // 같은 GameObject 에 Rigidbody2D 가 없으면 동작할 수 없어 스크립트를 끈다.
                Debug.LogError("PlayerController: Rigidbody2D 컴포넌트가 없습니다. 스크립트를 비활성화합니다.");
                enabled = false;
            }
        }
    }
}
```

이 파일의 자리는 `Assets/Scripts/Player/PlayerController.cs` 다.

## Unity 에 투입하기

생성한 소스를 **`contents` 인자로 직접 넘긴다.** 이러면 서버가 LLM 을 호출하지
않으므로 `ANTHROPIC_API_KEY` 가 필요 없다:

```
unity.create_script(
    featureId    = "spec-003",
    prompt       = "<사람이 읽을 기능 설명 — 추적용>",
    contents     = "<위에서 생성한 C# 전문>",
    plannedPath  = "Assets/Scripts/Player/PlayerController.cs",   # 설계안에서
    plannedClass = "PlayerController"                              # 설계안에서
)
```

> `contents` 를 비우면 서버가 스스로 LLM 을 호출하려다 키가 없어 실패한다.
> 이 스킬을 쓰는 동안에는 **항상 `contents` 를 채운다.**

**순회 단위는 spec 이 아니라 설계안의 파일이다.** spec 하나가 파일 셋이면 세 번
부른다 — `create_script` 는 언제나 파일 하나를 만든다. 순서는 설계안이
`dependsOn` 으로 이미 위상 정렬해 돌려준 그대로다.

반환의 `types` 에는 그 파일이 선언한 **모든** 타입이 들어 있다. 다음 호출의
`existingTypes` 에 그것을 누적해 넘긴다 — 첫 타입만 넘기면 같은 파일 안의 형제
타입이 뒤에 오는 파일에게 보이지 않아 재정의 컴파일 오류가 난다.

## 코드를 다 넣었으면 — 조립한다

여기서 멈추면 스크립트가 **어디에도 붙어 있지 않은** 상태로 빌드된다. 실제로
그 상태로 QA PASS 가 난 적이 있다 (06 문서 §1.2). 설계안의 `prefabs[]` 와
`scene` 을 그대로 실행한다.

```
# 1) 반복 등장물부터 — 씬이 프리팹을 인스턴스화하므로 순서가 있다
unity.create_prefab(gameId=…, prefabName="Enemy",
                    components=["EnemyBrain", "Rigidbody2D"],
                    sprite="Assets/Generated/enemy.png")

# 2) 씬 계층. 부모/자식 순서는 서버가 정리하므로 설계안 그대로 넘기면 된다
unity.compose_scene(gameId=…, sceneName="Main", objects=<설계안의 scene.objects>)

# 3) 인스펙터 참조 — 이게 있어야 그림이 화면에 나온다
unity.bind_reference(gameId=…, target="Assets/Prefabs/Enemy.prefab",
                     field="EnemyBrain.portrait", value="Assets/Generated/enemy.png")
```

- `target` 이 `.prefab` 으로 끝나면 프리팹, 아니면 씬 안의 계층 경로
  (`WorldRoot/Grid`)다. 씬 쪽이면 `scene="Main"` 도 함께 넘긴다.
- `field` 는 `"EnemySpawner.enemyPrefab"` 처럼 컴포넌트까지 못박는 쪽이 안전하다.
- `create_prefab` / `compose_scene` 의 `missing` 이 비어 있는지 확인한다. 값이
  있으면 그 이름의 타입을 Unity 가 못 찾은 것이다 — 대개 스크립트가 아직
  컴파일되지 않았다.

**절대 하지 말 것**: `Awake()` 에서 `new GameObject` + `AddComponent` 로 조립을
흉내 내는 것. 조립 도구가 없던 시절의 우회책이고, 지금은
`verify_project_layout.py` 의 L3 가 경고로 잡는다.

## 조립이 끝나면 어셈블리를 나눈다

```
unity.define_assemblies(gameId=…)          # apply=false 면 계획만 본다
```

카테고리 폴더마다 `.asmdef` 를 놓아 컴파일을 나눈다. 한 파일을 고칠 때 프로젝트
전체가 재컴파일되지 않으므로 **오류 수정 루프가 빨라진다.**

- **코드가 다 들어온 뒤에 부른다.** 설계안이 아니라 **실제 소스의 참조**를 읽어
  나누기 때문이다.
- **`skipped` 를 읽는다.** 나누지 못한 카테고리가 이유와 함께 나온다. 가장 흔한
  이유는 `Assets/Scripts` 최상위에 평면으로 놓인 파일이고, 그건 Folder Rule 을
  안 지켰다는 신호다 — 설계안 단계로 돌아가 카테고리를 주는 게 맞다.
- **나뉘지 않아도 고장이 아니다.** 서로 참조하는 카테고리는 한 `.asmdef` 로 합칠
  수 없어서 통째로 `Assembly-CSharp` 에 남는다. 덜 나뉜 상태는 느릴 뿐 깨지지
  않는다 — 억지로 나누면 순환 참조로 컴파일이 아예 안 된다.

## 다 됐으면 스스로 검사한다

```bash
cd Src/McpServers && python verify_project_layout.py "<Unity 프로젝트 경로>"
```

또는 `unity.inspect_project_layout(projectPath=…)`. **L1(문서 번호 이름)과
L2(미부착 MonoBehaviour), 그리고 L7(어셈블리 참조 순환)이 0건이어야 이 단계가
끝난 것이다.**

## 컴파일 오류를 고칠 때

1. `unity.get_compile_errors(gameId=...)` 로 오류 목록을 받는다
2. 오류가 가리키는 **파일에 해당하는 스크립트만** 다시 생성한다 — 전부 재생성하지 않는다
3. 재생성 시 프롬프트에 이전 실패를 명시한다:
   ```
   [Previous attempt failed] <message>
   [Location] <file>:<line>
   [Suggested fix] <suggested_fix>
   Regenerate the script so this error does not occur again.
   ```
   (형식은 `Src/DeveloperAI/app/graph/nodes/codegen.py::_error_context` 와 동일)
4. **`unity.create_script` 를 다시 부를 때 `previousSource` 에 그 파일의 현재
   전체 소스를 실어 보낸다.** 이게 있어야 서버가 `Unity_CreateScript` 에
   `Action: "Update"` 를 실어 보낸다 — 없이 부르면 Unity 가 "Script already
   exists ... Use 'update' action to modify" 로 거부한다. 그리고 이 인자가
   있어야 모델도 백지 재작성이 아니라 **수정**으로 프레이밍한다(고쳐진 곳
   옆이 새로 깨지지 않는다).
5. 같은 오류가 **3회 반복되면 멈추고 사람에게 보고한다.** 무한 재생성 금지 —
   오케스트레이터도 `DEVELOPMENT_QA_MAX_ITERATIONS` 로 같은 상한을 건다.
