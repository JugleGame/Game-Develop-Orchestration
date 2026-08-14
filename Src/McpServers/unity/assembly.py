"""조립 단계 — 설계안의 ``prefabs[]`` 와 ``scene`` 을 Unity 안에서 **실행**한다.

이 모듈은 아무것도 새로 판단하지 않는다. 무엇을 만들지는 이미
:mod:`unity.architecture` 가 정했고, 여기서는 그 결정을 Unity Editor 가 알아듣는
C# 한 장으로 옮길 뿐이다. 그래서 조립이 실행마다 흔들리지 않는다
(``docs/contracts.md``의 Unity 외부 효과 계약).

**왜 이 단계가 필요한가.** 원래 Unity 에서는 사람이 에디터에서 오브젝트를 만들고
스크립트를 끌어다 붙인다. 이 파이프라인에는 그 "붙이는" 도구가 없어서, 모델이
어쩔 수 없이 코드로 그 일을 대신했다 — 게임이 켜지는 순간 ``new GameObject`` 로
빈 상자를 만들고 ``AddComponent`` 로 스크립트를 꽂는 부트스트랩이다. 스크립트
여섯 장 중 다섯 장이 **어떤 GameObject 에도 붙어 있지 않은 채** 빌드되고 QA PASS
까지 진행되는 문제가 그 결과다. 이 세 도구가 생기면 그 우회가 더 이상
필요하지 않다.

``Unity_RunCommand`` 의 함정 네 가지를 이 모듈이 한 곳에서 흡수한다. 넷 다 이미
한 번씩 걸려 본 것들이라 :data:`RUN_COMMAND_RULES` 로 못박고 테스트가 지킨다.

1. 클래스는 반드시 ``internal class CommandScript : IRunCommand``. 다른 이름·
   접근성으로 보내면 로그도 없이 실패한다 ("No logs available").
2. 진입점은 ``Execute(ExecutionResult result)``.
3. ``using System.Reflection;`` 은 "unauthorized namespaces" 로 **컴파일 이전에**
   거부된다. 그래서 타입을 이름으로 찾을 때 리플렉션 대신
   ``UnityEditor.TypeCache`` 를 쓴다.
4. ``File.WriteAllText`` / ``File.Delete`` 는 **소스에 글자로 있기만 해도** 거부
   된다(호출 여부와 무관). 폴더 생성도 ``System.IO`` 가 아니라
   ``AssetDatabase.CreateFolder`` 로 한다.

또 Unity 는 이 코드를 ``Unity.AI...Editor`` 네임스페이스로 감싸므로, ``Unity.*``
와 이름이 겹칠 수 있는 타입은 전부 ``global::`` 로 못박는다.

**인자는 C# 소스에 그대로 박힌다.** 그래서 이 모듈은 값을 넣기 전에 문자 집합을
좁게 검사한다 — 따옴표·역슬래시·세미콜론이 통과하면 설계안의 문자열 하나가 남의
코드를 실행시키는 통로가 된다. 검사를 통과한 값은 이스케이프가 필요 없다.
"""

from __future__ import annotations

import re

# ``Unity_RunCommand`` 가 요구하는 세 조건. 테스트가 이 값으로 회귀를 막는다 —
# 셋 중 하나만 어긋나도 증상이 똑같이 "로그 없는 실패"라 원인을 찾기 어렵다.
RUN_COMMAND_RULES = (
    "internal class CommandScript : IRunCommand",
    "public void Execute(ExecutionResult result)",
)

#: 소스에 글자로 있기만 해도 Unity 가 거부하는 것들.
FORBIDDEN_SOURCE_TOKENS = (
    "System.Reflection",
    "File.WriteAllText",
    "File.Delete",
)

#: 결과 JSON 을 실어 보내는 표식. ``_extract_command_result`` 가 이걸로 찾는다.
RESULT_MARKER = "ASSEMBLY_RESULT"

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ASSET_PATH = re.compile(r"^Assets(?:/[A-Za-z0-9_][A-Za-z0-9_. -]*)+$")
_OBJECT_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*$")


class AssemblyError(ValueError):
    """조립 인자가 규칙을 어겼다. 도구가 계약의 VALIDATION_ERROR로 옮긴다."""


# ---------------------------------------------------------------------------
# 인자 검사 — 값이 C# 소스에 박히기 전에 통과해야 하는 문
# ---------------------------------------------------------------------------
def require_name(value: str, label: str) -> str:
    """GameObject·프리팹 이름. 공백과 기호를 받지 않는다.

    설계안이 이미 PascalCase 로 이름을 짓게 돼 있으므로 좁혀도 잃는 것이 없고,
    좁혀 두면 이 값이 C# 문자열 리터럴을 빠져나갈 방법이 사라진다.
    """

    text = (value or "").strip()
    if not _NAME.match(text):
        raise AssemblyError(f"{label} 은 영문자로 시작하는 식별자여야 합니다 (받은 값: {value!r})")
    return text


def require_type_name(value: str, label: str) -> str:
    """컴포넌트 타입 이름. ``Rigidbody2D`` 도 ``Game.Gameplay.PlayerBody`` 도 받는다."""

    text = (value or "").strip()
    if not _TYPE_NAME.match(text):
        raise AssemblyError(f"{label} 이 C# 타입 이름 형식이 아닙니다 (받은 값: {value!r})")
    return text


def require_asset_path(value: str, label: str, suffix: str = "") -> str:
    """``Assets/`` 아래 경로. 프로젝트 밖을 가리킬 수 없다."""

    text = (value or "").strip().replace("\\", "/")
    if not _ASSET_PATH.match(text):
        raise AssemblyError(
            f"{label} 은 Assets/ 로 시작하는 프로젝트 내부 경로여야 합니다 (받은 값: {value!r})"
        )
    if suffix and not text.endswith(suffix):
        raise AssemblyError(f"{label} 은 '{suffix}' 로 끝나야 합니다 (받은 값: {value!r})")
    return text


def require_object_path(value: str, label: str) -> str:
    """씬 안의 계층 경로 (``WorldRoot/Grid``)."""

    text = (value or "").strip()
    if not _OBJECT_PATH.match(text):
        raise AssemblyError(
            f"{label} 은 'Parent/Child' 형태의 씬 계층 경로여야 합니다 (받은 값: {value!r})"
        )
    return text


def order_objects(objects: list[dict[str, object]]) -> list[dict[str, object]]:
    """부모가 자식보다 먼저 오도록 정렬한다.

    설계 단계는 "부모가 씬에 존재하는가"만 보고 순서는 보지 않는다. 그런데 조립은
    부모를 먼저 만들어야 ``SetParent`` 가 성립하므로, 순서를 여기서 확정한다.
    순환이면 만들 수 없는 계층이라 거부한다.
    """

    remaining = list(objects)
    placed: set[str] = set()
    ordered: list[dict[str, object]] = []

    while remaining:
        progressed = False
        for entry in list(remaining):
            parent = str(entry.get("parent") or "")
            if parent and parent not in placed:
                continue
            ordered.append(entry)
            placed.add(str(entry["name"]))
            remaining.remove(entry)
            progressed = True
        if not progressed:
            stuck = ", ".join(str(entry["name"]) for entry in remaining)
            raise AssemblyError(f"씬 계층에 순환이 있거나 부모가 씬에 없습니다: {stuck}")
    return ordered


# ---------------------------------------------------------------------------
# C# 조각 — 세 명령이 공유한다
# ---------------------------------------------------------------------------
def _literal(value: str) -> str:
    """검사를 통과한 값만 들어오므로 이스케이프가 필요 없다."""

    return f'"{value}"'


def _string_array(values: list[str]) -> str:
    return ", ".join(_literal(item) for item in values)


# 공유 헬퍼. ``System.Reflection`` 없이 이름으로 타입을 찾고, ``System.IO`` 없이
# 폴더를 만든다 — 둘 다 ``Unity_RunCommand`` 가 정적으로 막는 것들이다.
_HELPERS = """
    private static System.Type FindComponentType(string name)
    {
        foreach (var type in global::UnityEditor.TypeCache
            .GetTypesDerivedFrom<global::UnityEngine.Component>())
        {
            if (type.Name == name || type.FullName == name)
            {
                return type;
            }
        }
        return null;
    }

    private static void EnsureFolder(string assetPath)
    {
        int cut = assetPath.LastIndexOf('/');
        if (cut < 0)
        {
            return;
        }
        string folder = assetPath.Substring(0, cut);
        if (global::UnityEditor.AssetDatabase.IsValidFolder(folder))
        {
            return;
        }
        string[] parts = folder.Split('/');
        string current = parts[0];
        for (int i = 1; i < parts.Length; i++)
        {
            string next = current + "/" + parts[i];
            if (!global::UnityEditor.AssetDatabase.IsValidFolder(next))
            {
                global::UnityEditor.AssetDatabase.CreateFolder(current, parts[i]);
            }
            current = next;
        }
    }

    private static string JsonArray(System.Collections.Generic.List<string> items)
    {
        var builder = new System.Text.StringBuilder("[");
        for (int i = 0; i < items.Count; i++)
        {
            if (i > 0)
            {
                builder.Append(",");
            }
            builder.Append("\\"").Append(items[i]).Append("\\"");
        }
        return builder.Append("]").ToString();
    }
"""


def _wrap(body: str) -> str:
    """``Unity_RunCommand`` 계약을 지키는 껍데기를 씌운다."""

    return (
        "using UnityEngine;\n\n"
        "internal class CommandScript : IRunCommand\n"
        "{\n"
        "    public void Execute(ExecutionResult result)\n"
        "    {\n"
        f"{body}\n"
        "    }\n"
        f"{_HELPERS}"
        "}\n"
    )


# ---------------------------------------------------------------------------
# create_prefab
# ---------------------------------------------------------------------------
def prefab_command(
    prefab_name: str,
    prefab_path: str,
    components: list[str],
    sprite: str,
    model: str = "",
) -> str:
    """오브젝트를 만들고 컴포넌트를 붙여 ``.prefab`` 으로 저장하는 C#."""

    body = f"""        string prefabPath = {_literal(prefab_path)};
        string spritePath = {_literal(sprite)};
        string modelPath = {_literal(model)};
        string[] wanted = new string[] {{ {_string_array(components)} }};

        var attached = new System.Collections.Generic.List<string>();
        var missing = new System.Collections.Generic.List<string>();

        EnsureFolder(prefabPath);
        global::UnityEngine.GameObject root = null;
        if (modelPath.Length > 0)
        {{
            var modelAsset = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEngine.GameObject>(modelPath);
            if (modelAsset != null)
            {{
                root = (global::UnityEngine.GameObject)
                    global::UnityEditor.PrefabUtility.InstantiatePrefab(modelAsset);
            }}
            else
            {{
                missing.Add(modelPath);
            }}
        }}
        if (root == null)
        {{
            root = new global::UnityEngine.GameObject({_literal(prefab_name)});
        }}
        root.name = {_literal(prefab_name)};

        foreach (var name in wanted)
        {{
            var type = FindComponentType(name);
            if (type == null)
            {{
                missing.Add(name);
                continue;
            }}
            if (root.GetComponent(type) == null)
            {{
                root.AddComponent(type);
            }}
            attached.Add(name);
        }}

        if (spritePath.Length > 0)
        {{
            var renderer = root.GetComponent<global::UnityEngine.SpriteRenderer>();
            if (renderer == null)
            {{
                renderer = root.AddComponent<global::UnityEngine.SpriteRenderer>();
            }}
            var sprite = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEngine.Sprite>(spritePath);
            if (sprite != null)
            {{
                renderer.sprite = sprite;
            }}
            else
            {{
                missing.Add(spritePath);
            }}
        }}

        var saved = global::UnityEditor.PrefabUtility.SaveAsPrefabAsset(root, prefabPath);
        global::UnityEngine.Object.DestroyImmediate(root);
        global::UnityEditor.AssetDatabase.SaveAssets();

        bool success = saved != null && (modelPath.Length == 0 || !missing.Contains(modelPath));
        string payload = "{{\\"success\\":" + (success ? "true" : "false")
            + ",\\"prefab\\":\\"" + prefabPath + "\\""
            + ",\\"attached\\":" + JsonArray(attached)
            + ",\\"missing\\":" + JsonArray(missing)
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


# ---------------------------------------------------------------------------
# compose_scene
# ---------------------------------------------------------------------------
def scene_command(scene_path: str, objects: list[dict[str, object]]) -> str:
    """씬 계층을 실제로 만들고 저장한 뒤 빌드 설정에 등록하는 C#.

    빌드 설정 등록이 덤처럼 보이지만 아니다 — ``build_project`` 의 C# 은 활성
    씬이 하나도 없으면 그 자리에서 실패한다. 씬을 만든 도구가 등록까지 하지
    않으면 그 실패를 빌드 단계에서야 만나게 된다.
    """

    names = [str(entry["name"]) for entry in objects]
    parents = [str(entry.get("parent") or "") for entry in objects]
    prefabs = [str(entry.get("prefab") or "") for entry in objects]
    component_sets = [
        ";".join(str(item) for item in entry.get("components") or []) for entry in objects
    ]

    body = f"""        string scenePath = {_literal(scene_path)};
        string[] names = new string[] {{ {_string_array(names)} }};
        string[] parents = new string[] {{ {_string_array(parents)} }};
        string[] prefabs = new string[] {{ {_string_array(prefabs)} }};
        string[] componentSets = new string[] {{ {_string_array(component_sets)} }};

        var created = new System.Collections.Generic.Dictionary<string, global::UnityEngine.GameObject>();
        var attached = new System.Collections.Generic.List<string>();
        var missing = new System.Collections.Generic.List<string>();

        var scene = global::UnityEditor.SceneManagement.EditorSceneManager.NewScene(
            global::UnityEditor.SceneManagement.NewSceneSetup.DefaultGameObjects,
            global::UnityEditor.SceneManagement.NewSceneMode.Single);

        for (int i = 0; i < names.Length; i++)
        {{
            global::UnityEngine.GameObject go = null;
            if (prefabs[i].Length > 0)
            {{
                var asset = global::UnityEditor.AssetDatabase
                    .LoadAssetAtPath<global::UnityEngine.GameObject>(prefabs[i]);
                if (asset == null)
                {{
                    missing.Add(prefabs[i]);
                    continue;
                }}
                go = (global::UnityEngine.GameObject)
                    global::UnityEditor.PrefabUtility.InstantiatePrefab(asset);
            }}
            else
            {{
                go = new global::UnityEngine.GameObject(names[i]);
            }}

            go.name = names[i];
            created[names[i]] = go;

            if (parents[i].Length > 0 && created.ContainsKey(parents[i]))
            {{
                go.transform.SetParent(created[parents[i]].transform, false);
            }}

            if (componentSets[i].Length > 0)
            {{
                foreach (var name in componentSets[i].Split(';'))
                {{
                    var type = FindComponentType(name);
                    if (type == null)
                    {{
                        missing.Add(name);
                        continue;
                    }}
                    if (go.GetComponent(type) == null)
                    {{
                        go.AddComponent(type);
                    }}
                    attached.Add(names[i] + "." + name);
                }}
            }}
        }}

        EnsureFolder(scenePath);
        bool saved = global::UnityEditor.SceneManagement.EditorSceneManager
            .SaveScene(scene, scenePath);

        var entries = new System.Collections.Generic.List<global::UnityEditor.EditorBuildSettingsScene>(
            global::UnityEditor.EditorBuildSettings.scenes);
        bool registered = false;
        foreach (var entry in entries)
        {{
            if (entry.path == scenePath)
            {{
                entry.enabled = true;
                registered = true;
            }}
        }}
        if (!registered)
        {{
            entries.Insert(0, new global::UnityEditor.EditorBuildSettingsScene(scenePath, true));
        }}
        global::UnityEditor.EditorBuildSettings.scenes = entries.ToArray();

        string payload = "{{\\"success\\":" + (saved ? "true" : "false")
            + ",\\"scene\\":\\"" + scenePath + "\\""
            + ",\\"objects\\":" + created.Count
            + ",\\"attached\\":" + JsonArray(attached)
            + ",\\"missing\\":" + JsonArray(missing)
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


# ---------------------------------------------------------------------------
# bind_reference
# ---------------------------------------------------------------------------
def bind_command(
    target: str, type_name: str, field_name: str, value_path: str, scene_path: str
) -> str:
    """인스펙터의 ``[SerializeField]`` 칸에 스프라이트·프리팹을 꽂는 C#.

    ``SerializedObject`` / ``SerializedProperty`` 로만 쓴다 — 필드에 직접 값을
    넣으려면 리플렉션이 필요한데 그 네임스페이스가 막혀 있고, 애초에 에디터에서
    직렬화 값을 바꾸는 정식 경로가 이쪽이다.
    """

    is_prefab = target.endswith(".prefab")
    body = f"""        string target = {_literal(target)};
        string typeName = {_literal(type_name)};
        string fieldName = {_literal(field_name)};
        string valuePath = {_literal(value_path)};
        string scenePath = {_literal(scene_path)};

        var asset = global::UnityEditor.AssetDatabase
            .LoadAssetAtPath<global::UnityEngine.Object>(valuePath);
        var sprite = global::UnityEditor.AssetDatabase
            .LoadAssetAtPath<global::UnityEngine.Sprite>(valuePath);
        if (asset == null && sprite == null)
        {{
            result.Log("{RESULT_MARKER} {{0}}",
                "{{\\"success\\":false,\\"message\\":\\"asset not found\\"}}");
            return;
        }}

        global::UnityEngine.GameObject root = null;
        {"root = global::UnityEditor.PrefabUtility.LoadPrefabContents(target);" if is_prefab else '''if (scenePath.Length > 0)
        {
            global::UnityEditor.SceneManagement.EditorSceneManager.OpenScene(
                scenePath, global::UnityEditor.SceneManagement.OpenSceneMode.Single);
        }
        root = global::UnityEngine.GameObject.Find(target);'''}
        if (root == null)
        {{
            result.Log("{RESULT_MARKER} {{0}}",
                "{{\\"success\\":false,\\"message\\":\\"target not found\\"}}");
            return;
        }}

        bool bound = false;
        string boundType = "";
        foreach (var component in root.GetComponents<global::UnityEngine.Component>())
        {{
            if (component == null)
            {{
                continue;
            }}
            if (typeName.Length > 0 && component.GetType().Name != typeName)
            {{
                continue;
            }}
            var serialized = new global::UnityEditor.SerializedObject(component);
            var property = serialized.FindProperty(fieldName);
            if (property == null
                || property.propertyType
                    != global::UnityEditor.SerializedPropertyType.ObjectReference)
            {{
                continue;
            }}
            bool wantsSprite = property.type.Contains("Sprite");
            property.objectReferenceValue = (wantsSprite && sprite != null)
                ? (global::UnityEngine.Object)sprite
                : asset;
            serialized.ApplyModifiedPropertiesWithoutUndo();
            boundType = component.GetType().Name;
            bound = true;
            break;
        }}

        if (bound)
        {{
            {"global::UnityEditor.PrefabUtility.SaveAsPrefabAsset(root, target);" if is_prefab else "global::UnityEditor.SceneManagement.EditorSceneManager.SaveScene(root.scene);"}
            global::UnityEditor.AssetDatabase.SaveAssets();
        }}
        {"global::UnityEditor.PrefabUtility.UnloadPrefabContents(root);" if is_prefab else ""}

        string payload = "{{\\"success\\":" + (bound ? "true" : "false")
            + ",\\"target\\":\\"" + target + "\\""
            + ",\\"field\\":\\"" + fieldName + "\\""
            + ",\\"component\\":\\"" + boundType + "\\""
            + ",\\"value\\":\\"" + valuePath + "\\""
            + (bound ? "" : ",\\"message\\":\\"no serialized field matched\\"")
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


def split_field(field: str) -> tuple[str, str]:
    """``"EnemySpawner.enemyPrefab"`` → ``("EnemySpawner", "enemyPrefab")``.

    타입을 생략하면 그 오브젝트의 컴포넌트를 순서대로 훑어 그 이름의 직렬화
    필드를 가진 첫 컴포넌트에 꽂는다. 같은 이름의 필드를 두 컴포넌트가 함께
    가지는 일이 드물어 실용적이지만, 확실히 하려면 타입을 적는 쪽이 낫다.
    """

    text = (field or "").strip()
    if "." in text:
        type_name, _, field_name = text.rpartition(".")
        return require_name(type_name, "field 의 타입 부분"), require_name(
            field_name, "field 의 필드 부분"
        )
    return "", require_name(text, "field")
