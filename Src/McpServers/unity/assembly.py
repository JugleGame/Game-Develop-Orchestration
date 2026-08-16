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


def require_vector3(value: object, label: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    """Validate a deterministic scene transform vector."""
    if value is None:
        return default
    if not isinstance(value, list) or len(value) != 3 or any(
        not isinstance(item, (int, float)) or isinstance(item, bool) for item in value
    ):
        raise AssemblyError(f"{label} 은 숫자 3개짜리 배열이어야 합니다")
    return tuple(float(item) for item in value)


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
    collider_size: tuple[float, float] | None = None,
    collider_offset: tuple[float, float] | None = None,
    sprite_pivot: tuple[float, float] | None = None,
) -> str:
    """오브젝트를 만들고 컴포넌트를 붙여 ``.prefab`` 으로 저장하는 C#.

    콜라이더 치수와 피벗을 함께 받는 이유는 그것들이 **코드가 가정하는 몸 크기**
    와 같은 값이기 때문이다. 따로 두면 스프라이트 2x4 유닛에 콜라이더 1x1 이
    붙는 상태가 아무 신호 없이 만들어진다(실제로 그렇게 됐다).
    """

    size_x, size_y = collider_size if collider_size else (0.0, 0.0)
    offset_x, offset_y = collider_offset if collider_offset else (0.0, 0.0)
    pivot_x, pivot_y = sprite_pivot if sprite_pivot else (0.0, 0.0)

    body = f"""        string prefabPath = {_literal(prefab_path)};
        string spritePath = {_literal(sprite)};
        string modelPath = {_literal(model)};
        string[] wanted = new string[] {{ {_string_array(components)} }};
        bool hasColliderSize = {"true" if collider_size else "false"};
        bool hasColliderOffset = {"true" if collider_offset else "false"};
        bool hasPivot = {"true" if sprite_pivot else "false"};
        var colliderSize = new global::UnityEngine.Vector2({float(size_x)}f, {float(size_y)}f);
        var colliderOffset = new global::UnityEngine.Vector2({float(offset_x)}f, {float(offset_y)}f);
        var spritePivot = new global::UnityEngine.Vector2({float(pivot_x)}f, {float(pivot_y)}f);
        string colliderApplied = "";

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

        if (hasColliderSize || hasColliderOffset)
        {{
            var collider = root.GetComponent<global::UnityEngine.Collider2D>();
            if (collider == null)
            {{
                missing.Add("Collider2D");
            }}
            else
            {{
                if (hasColliderOffset)
                {{
                    collider.offset = colliderOffset;
                }}
                if (hasColliderSize)
                {{
                    var capsule = collider as global::UnityEngine.CapsuleCollider2D;
                    var box = collider as global::UnityEngine.BoxCollider2D;
                    var circle = collider as global::UnityEngine.CircleCollider2D;
                    if (capsule != null)
                    {{
                        capsule.size = colliderSize;
                    }}
                    else if (box != null)
                    {{
                        box.size = colliderSize;
                    }}
                    else if (circle != null)
                    {{
                        circle.radius = colliderSize.x / 2f;
                    }}
                    else
                    {{
                        missing.Add(collider.GetType().Name);
                    }}
                }}
                colliderApplied = collider.GetType().Name;
            }}
        }}

        if (hasPivot && spritePath.Length > 0)
        {{
            var textureImporter = global::UnityEditor.AssetImporter.GetAtPath(spritePath)
                as global::UnityEditor.TextureImporter;
            if (textureImporter == null)
            {{
                missing.Add(spritePath);
            }}
            else
            {{
                var textureSettings = new global::UnityEditor.TextureImporterSettings();
                textureImporter.ReadTextureSettings(textureSettings);
                textureSettings.spriteAlignment =
                    (int)global::UnityEngine.SpriteAlignment.Custom;
                textureSettings.spritePivot = spritePivot;
                textureImporter.SetTextureSettings(textureSettings);
                textureImporter.SaveAndReimport();
            }}
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
            + ",\\"collider\\":\\"" + colliderApplied + "\\""
            + ",\\"missing\\":" + JsonArray(missing)
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


# ---------------------------------------------------------------------------
# import_asset
# ---------------------------------------------------------------------------
def texture_import_command(folder: str, model_path: str, max_texture_size: int) -> str:
    """사이드카 텍스처를 올바른 타입·해상도로 읽고 URP 머티리얼에 묶는 C#.

    세 가지가 기본값으로는 어긋난다. 노멀맵이 ``Default`` + sRGB 로 들어오면
    ``_BumpMap`` 에서 면이 갈라져 보이고, 2048 짜리 맵이 그대로 WebGL 빌드에
    들어가며, FBX 임포터는 base color 와 normal 만 연결해 metallic/smoothness 가
    빠진다. 파일 이름이 유일한 단서라 이름으로 판정한다.
    """

    body = f"""        string folder = {_literal(folder)};
        string modelPath = {_literal(model_path)};
        int maxTextureSize = {int(max_texture_size)};
        var repaired = new System.Collections.Generic.List<string>();

        global::UnityEngine.Texture2D baseMap = null;
        global::UnityEngine.Texture2D normalMap = null;
        global::UnityEngine.Texture2D metallicMap = null;
        global::UnityEngine.Texture2D emissionMap = null;

        foreach (var guid in global::UnityEditor.AssetDatabase
            .FindAssets("t:Texture2D", new string[] {{ folder }}))
        {{
            string path = global::UnityEditor.AssetDatabase.GUIDToAssetPath(guid);
            var importer = global::UnityEditor.AssetImporter.GetAtPath(path)
                as global::UnityEditor.TextureImporter;
            if (importer == null)
            {{
                continue;
            }}
            string lower = path.ToLowerInvariant();
            bool isNormal = lower.Contains("normal");
            bool isMetallic = lower.Contains("metallicsmoothness");
            bool isEmission = lower.Contains("emission");
            bool isLinear = isNormal || isMetallic || lower.Contains("metallic")
                || lower.Contains("roughness") || lower.Contains("occlusion");
            bool isBaseColor = !isLinear && !isEmission;
            bool changed = false;

            if (isNormal && importer.textureType != global::UnityEditor.TextureImporterType.NormalMap)
            {{
                importer.textureType = global::UnityEditor.TextureImporterType.NormalMap;
                changed = true;
            }}
            if (isLinear && !isNormal && importer.sRGBTexture)
            {{
                importer.sRGBTexture = false;
                changed = true;
            }}
            if (isMetallic && !importer.alphaIsTransparency)
            {{
                importer.alphaSource = global::UnityEditor.TextureImporterAlphaSource.FromInput;
                changed = true;
            }}

            // Only the base color is read at full size; the rest carry lower-frequency data.
            int budget = isBaseColor ? maxTextureSize : System.Math.Max(128, maxTextureSize / 2);
            if (importer.maxTextureSize > budget)
            {{
                importer.maxTextureSize = budget;
                changed = true;
            }}
            if (importer.textureCompression != global::UnityEditor.TextureImporterCompression.Compressed)
            {{
                importer.textureCompression = global::UnityEditor.TextureImporterCompression.Compressed;
                changed = true;
            }}
            // Crunch trades import time for a much smaller download, which is what WebGL pays for.
            if (!importer.crunchedCompression)
            {{
                importer.crunchedCompression = true;
                importer.compressionQuality = 50;
                changed = true;
            }}

            bool needsAlpha = isMetallic || isNormal || importer.DoesSourceTextureHaveAlpha();
            var webgl = importer.GetPlatformTextureSettings("WebGL");
            var wanted = needsAlpha
                ? global::UnityEditor.TextureImporterFormat.DXT5Crunched
                : global::UnityEditor.TextureImporterFormat.DXT1Crunched;
            if (!webgl.overridden || webgl.format != wanted || webgl.maxTextureSize != budget)
            {{
                webgl.overridden = true;
                webgl.format = wanted;
                webgl.maxTextureSize = budget;
                webgl.textureCompression = global::UnityEditor.TextureImporterCompression.Compressed;
                webgl.crunchedCompression = true;
                webgl.compressionQuality = 50;
                importer.SetPlatformTextureSettings(webgl);
                changed = true;
            }}

            if (changed)
            {{
                importer.SaveAndReimport();
                repaired.Add(path);
            }}

            var texture = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEngine.Texture2D>(path);
            if (isNormal)
            {{
                normalMap = texture;
            }}
            else if (isMetallic)
            {{
                metallicMap = texture;
            }}
            else if (lower.Contains("emission"))
            {{
                emissionMap = texture;
            }}
            else if (!lower.Contains("metallic") && !lower.Contains("roughness")
                && !lower.Contains("occlusion"))
            {{
                baseMap = texture;
            }}
        }}

        string materialPath = "";
        var modelImporter = global::UnityEditor.AssetImporter.GetAtPath(modelPath)
            as global::UnityEditor.ModelImporter;
        if (modelImporter != null && baseMap != null)
        {{
            var shader = global::UnityEngine.Shader.Find("Universal Render Pipeline/Lit");
            if (shader == null)
            {{
                shader = global::UnityEngine.Shader.Find("Standard");
            }}
            materialPath = folder + "/" + baseMap.name + ".mat";
            var material = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEngine.Material>(materialPath);
            if (material == null)
            {{
                material = new global::UnityEngine.Material(shader);
                global::UnityEditor.AssetDatabase.CreateAsset(material, materialPath);
            }}
            material.shader = shader;
            material.SetTexture("_BaseMap", baseMap);
            material.SetTexture("_MainTex", baseMap);
            if (normalMap != null)
            {{
                material.SetTexture("_BumpMap", normalMap);
                material.EnableKeyword("_NORMALMAP");
            }}
            if (metallicMap != null)
            {{
                material.SetTexture("_MetallicGlossMap", metallicMap);
                material.SetFloat("_Metallic", 1f);
                material.SetFloat("_Smoothness", 1f);
                material.SetFloat("_GlossMapScale", 1f);
                material.EnableKeyword("_METALLICSPECGLOSSMAP");
            }}
            if (emissionMap != null)
            {{
                material.SetTexture("_EmissionMap", emissionMap);
                material.SetColor("_EmissionColor", global::UnityEngine.Color.white);
                material.EnableKeyword("_EMISSION");
            }}
            global::UnityEditor.EditorUtility.SetDirty(material);

            foreach (var entry in modelImporter.GetExternalObjectMap())
            {{
                modelImporter.RemoveRemap(entry.Key);
            }}
            foreach (var asset in global::UnityEditor.AssetDatabase.LoadAllAssetsAtPath(modelPath))
            {{
                var embedded = asset as global::UnityEngine.Material;
                if (embedded == null || embedded == material)
                {{
                    continue;
                }}
                modelImporter.AddRemap(
                    new global::UnityEditor.AssetImporter.SourceAssetIdentifier(
                        typeof(global::UnityEngine.Material), embedded.name),
                    material);
            }}
            global::UnityEditor.AssetDatabase.WriteImportSettingsIfDirty(modelPath);
            global::UnityEditor.AssetDatabase.ImportAsset(
                modelPath, global::UnityEditor.ImportAssetOptions.ForceUpdate);
            global::UnityEditor.AssetDatabase.SaveAssets();
        }}

        string payload = "{{\\"success\\":true,\\"repaired\\":" + JsonArray(repaired)
            + ",\\"material\\":\\"" + materialPath + "\\"}}";
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
    positions = [require_vector3(entry.get("position"), "position", (0.0, 0.0, 0.0)) for entry in objects]
    rotations = [require_vector3(entry.get("rotation"), "rotation", (0.0, 0.0, 0.0)) for entry in objects]
    scales = [require_vector3(entry.get("scale"), "scale", (1.0, 1.0, 1.0)) for entry in objects]
    vector_literals = lambda values: ", ".join(
        f"new global::UnityEngine.Vector3({x}f, {y}f, {z}f)" for x, y, z in values
    )

    body = f"""        string scenePath = {_literal(scene_path)};
        string[] names = new string[] {{ {_string_array(names)} }};
        string[] parents = new string[] {{ {_string_array(parents)} }};
        string[] prefabs = new string[] {{ {_string_array(prefabs)} }};
        string[] componentSets = new string[] {{ {_string_array(component_sets)} }};
        global::UnityEngine.Vector3[] positions = new global::UnityEngine.Vector3[] {{ {vector_literals(positions)} }};
        global::UnityEngine.Vector3[] rotations = new global::UnityEngine.Vector3[] {{ {vector_literals(rotations)} }};
        global::UnityEngine.Vector3[] scales = new global::UnityEngine.Vector3[] {{ {vector_literals(scales)} }};

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
            go.transform.position = positions[i];
            go.transform.rotation = global::UnityEngine.Quaternion.Euler(rotations[i]);
            go.transform.localScale = scales[i];
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


# ---------------------------------------------------------------------------
# create_animation_clip / create_animator_controller / inspect_animator
# ---------------------------------------------------------------------------
#: Animator parameter kinds the controller builder accepts.
PARAMETER_TYPES = ("Float", "Int", "Bool", "Trigger")

#: Condition modes, mapped onto ``AnimatorConditionMode``.
CONDITION_MODES = ("If", "IfNot", "Greater", "Less", "Equals", "NotEqual")


def _float_array(values: list[float]) -> str:
    return ", ".join(f"{float(value)}f" for value in values)


def _int_array(values: list[int]) -> str:
    return ", ".join(str(int(value)) for value in values)


def _bool_array(values: list[bool]) -> str:
    return ", ".join("true" if value else "false" for value in values)


def animation_clip_command(
    clip_path: str,
    frame_paths: list[str],
    frames_per_second: float,
    loop: bool,
) -> str:
    """낱장 스프라이트 프레임을 재생 순서 그대로 클립 하나로 묶는 C#.

    프레임은 ``SpriteRenderer.m_Sprite`` 오브젝트 참조 커브로 들어간다 —
    스프라이트 애니메이션은 값이 아니라 참조가 바뀌는 것이라 일반
    ``AnimationCurve`` 로는 표현되지 않는다.
    """

    body = f"""        string clipPath = {_literal(clip_path)};
        string[] framePaths = new string[] {{ {_string_array(frame_paths)} }};
        float fps = {float(frames_per_second)}f;
        bool loop = {"true" if loop else "false"};

        var missing = new System.Collections.Generic.List<string>();
        var sprites = new System.Collections.Generic.List<global::UnityEngine.Sprite>();
        foreach (var path in framePaths)
        {{
            var sprite = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEngine.Sprite>(path);
            if (sprite == null)
            {{
                missing.Add(path);
                continue;
            }}
            sprites.Add(sprite);
        }}

        if (missing.Count > 0 || sprites.Count == 0)
        {{
            string failure = "{{\\"success\\":false"
                + ",\\"clip\\":\\"" + clipPath + "\\""
                + ",\\"missing\\":" + JsonArray(missing)
                + "}}";
            result.Log("{RESULT_MARKER} {{0}}", failure);
            return;
        }}

        EnsureFolder(clipPath);
        var clip = new global::UnityEngine.AnimationClip();
        clip.frameRate = fps;

        var binding = global::UnityEditor.EditorCurveBinding.PPtrCurve(
            "", typeof(global::UnityEngine.SpriteRenderer), "m_Sprite");
        var keys = new global::UnityEditor.ObjectReferenceKeyframe[sprites.Count];
        for (int i = 0; i < sprites.Count; i++)
        {{
            keys[i] = new global::UnityEditor.ObjectReferenceKeyframe();
            keys[i].time = i / fps;
            keys[i].value = sprites[i];
        }}
        global::UnityEditor.AnimationUtility.SetObjectReferenceCurve(clip, binding, keys);

        var settings = global::UnityEditor.AnimationUtility.GetAnimationClipSettings(clip);
        settings.loopTime = loop;
        global::UnityEditor.AnimationUtility.SetAnimationClipSettings(clip, settings);

        global::UnityEditor.AssetDatabase.CreateAsset(clip, clipPath);
        global::UnityEditor.AssetDatabase.SaveAssets();

        string payload = "{{\\"success\\":true"
            + ",\\"clip\\":\\"" + clipPath + "\\""
            + ",\\"frameCount\\":" + sprites.Count
            + ",\\"frameRate\\":" + clip.frameRate
            + ",\\"loop\\":" + (loop ? "true" : "false")
            + ",\\"length\\":" + clip.length
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


def animator_controller_command(
    controller_path: str,
    state_names: list[str],
    state_clips: list[str],
    parameter_names: list[str],
    parameter_types: list[str],
    default_state: str,
    transition_from: list[str],
    transition_to: list[str],
    transition_durations: list[float],
    transition_has_exit: list[bool],
    condition_transition: list[int],
    condition_parameters: list[str],
    condition_modes: list[str],
    condition_thresholds: list[float],
    target_prefab: str = "",
) -> str:
    """상태 전환 그래프를 만들고, 요청하면 프리팹의 ``Animator`` 에 꽂는 C#.

    조건은 전환마다 개수가 달라서 하나의 평평한 배열로 보내고 전환 인덱스로
    묶는다 — 중첩 구조를 C# 소스에 글자로 박는 것보다 검사하기 쉽다.
    """

    body = f"""        string controllerPath = {_literal(controller_path)};
        string prefabPath = {_literal(target_prefab)};
        string[] stateNames = new string[] {{ {_string_array(state_names)} }};
        string[] stateClips = new string[] {{ {_string_array(state_clips)} }};
        string[] parameterNames = new string[] {{ {_string_array(parameter_names)} }};
        string[] parameterTypes = new string[] {{ {_string_array(parameter_types)} }};
        string defaultState = {_literal(default_state)};
        string[] fromStates = new string[] {{ {_string_array(transition_from)} }};
        string[] toStates = new string[] {{ {_string_array(transition_to)} }};
        float[] durations = new float[] {{ {_float_array(transition_durations)} }};
        bool[] hasExitTime = new bool[] {{ {_bool_array(transition_has_exit)} }};
        int[] conditionOwner = new int[] {{ {_int_array(condition_transition)} }};
        string[] conditionParameters = new string[] {{ {_string_array(condition_parameters)} }};
        string[] conditionModes = new string[] {{ {_string_array(condition_modes)} }};
        float[] conditionThresholds = new float[] {{ {_float_array(condition_thresholds)} }};

        var missing = new System.Collections.Generic.List<string>();
        EnsureFolder(controllerPath);
        var controller = global::UnityEditor.Animations.AnimatorController
            .CreateAnimatorControllerAtPath(controllerPath);
        var layer = controller.layers[0];
        var machine = layer.stateMachine;

        for (int i = 0; i < parameterNames.Length; i++)
        {{
            var kind = global::UnityEngine.AnimatorControllerParameterType.Float;
            if (parameterTypes[i] == "Int")
            {{
                kind = global::UnityEngine.AnimatorControllerParameterType.Int;
            }}
            else if (parameterTypes[i] == "Bool")
            {{
                kind = global::UnityEngine.AnimatorControllerParameterType.Bool;
            }}
            else if (parameterTypes[i] == "Trigger")
            {{
                kind = global::UnityEngine.AnimatorControllerParameterType.Trigger;
            }}
            controller.AddParameter(parameterNames[i], kind);
        }}

        var states = new System.Collections.Generic.Dictionary<
            string, global::UnityEditor.Animations.AnimatorState>();
        for (int i = 0; i < stateNames.Length; i++)
        {{
            var state = machine.AddState(stateNames[i]);
            if (stateClips[i].Length > 0)
            {{
                var clip = global::UnityEditor.AssetDatabase
                    .LoadAssetAtPath<global::UnityEngine.AnimationClip>(stateClips[i]);
                if (clip == null)
                {{
                    missing.Add(stateClips[i]);
                }}
                else
                {{
                    state.motion = clip;
                }}
            }}
            states[stateNames[i]] = state;
            if (stateNames[i] == defaultState)
            {{
                machine.defaultState = state;
            }}
        }}

        var transitions = new System.Collections.Generic.List<
            global::UnityEditor.Animations.AnimatorStateTransition>();
        for (int i = 0; i < fromStates.Length; i++)
        {{
            var transition = states[fromStates[i]].AddTransition(states[toStates[i]]);
            transition.hasExitTime = hasExitTime[i];
            transition.duration = durations[i];
            transitions.Add(transition);
        }}

        for (int i = 0; i < conditionOwner.Length; i++)
        {{
            var mode = global::UnityEditor.Animations.AnimatorConditionMode.If;
            if (conditionModes[i] == "IfNot")
            {{
                mode = global::UnityEditor.Animations.AnimatorConditionMode.IfNot;
            }}
            else if (conditionModes[i] == "Greater")
            {{
                mode = global::UnityEditor.Animations.AnimatorConditionMode.Greater;
            }}
            else if (conditionModes[i] == "Less")
            {{
                mode = global::UnityEditor.Animations.AnimatorConditionMode.Less;
            }}
            else if (conditionModes[i] == "Equals")
            {{
                mode = global::UnityEditor.Animations.AnimatorConditionMode.Equals;
            }}
            else if (conditionModes[i] == "NotEqual")
            {{
                mode = global::UnityEditor.Animations.AnimatorConditionMode.NotEqual;
            }}
            transitions[conditionOwner[i]].AddCondition(
                mode, conditionThresholds[i], conditionParameters[i]);
        }}

        bool bound = false;
        if (prefabPath.Length > 0)
        {{
            var root = global::UnityEditor.PrefabUtility.LoadPrefabContents(prefabPath);
            if (root == null)
            {{
                missing.Add(prefabPath);
            }}
            else
            {{
                var animator = root.GetComponent<global::UnityEngine.Animator>();
                if (animator == null)
                {{
                    animator = root.AddComponent<global::UnityEngine.Animator>();
                }}
                animator.runtimeAnimatorController = controller;
                global::UnityEditor.PrefabUtility.SaveAsPrefabAsset(root, prefabPath);
                global::UnityEditor.PrefabUtility.UnloadPrefabContents(root);
                bound = true;
            }}
        }}

        global::UnityEditor.AssetDatabase.SaveAssets();

        var stateList = new System.Collections.Generic.List<string>();
        foreach (var name in stateNames)
        {{
            stateList.Add(name);
        }}
        var parameterList = new System.Collections.Generic.List<string>();
        foreach (var name in parameterNames)
        {{
            parameterList.Add(name);
        }}

        string payload = "{{\\"success\\":" + (missing.Count == 0 ? "true" : "false")
            + ",\\"controller\\":\\"" + controllerPath + "\\""
            + ",\\"states\\":" + JsonArray(stateList)
            + ",\\"parameters\\":" + JsonArray(parameterList)
            + ",\\"transitions\\":" + transitions.Count
            + ",\\"boundToPrefab\\":" + (bound ? "true" : "false")
            + ",\\"missing\\":" + JsonArray(missing)
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


def animator_inspect_command(target: str) -> str:
    """컨트롤러 또는 프리팹 하나의 상태·파라미터·클립 프레임 수를 읽는 C#.

    자동 판정은 하지 않는다 — 무엇이 실려 있는지만 그대로 돌려주고 통과 여부는
    호스트가 판단한다 (``docs/contracts.md`` 의 "증거를 돌려주고 PASS 를 선언하지
    않는다").
    """

    body = f"""        string target = {_literal(target)};

        var states = new System.Collections.Generic.List<string>();
        var parameters = new System.Collections.Generic.List<string>();
        var clips = new System.Collections.Generic.List<string>();
        string controllerPath = "";
        bool hasAnimator = false;

        global::UnityEditor.Animations.AnimatorController controller = null;
        if (target.EndsWith(".prefab"))
        {{
            var root = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEngine.GameObject>(target);
            if (root != null)
            {{
                var animator = root.GetComponent<global::UnityEngine.Animator>();
                hasAnimator = animator != null;
                if (animator != null && animator.runtimeAnimatorController != null)
                {{
                    controllerPath = global::UnityEditor.AssetDatabase
                        .GetAssetPath(animator.runtimeAnimatorController);
                    controller = global::UnityEditor.AssetDatabase
                        .LoadAssetAtPath<global::UnityEditor.Animations.AnimatorController>(
                            controllerPath);
                }}
            }}
        }}
        else
        {{
            controllerPath = target;
            controller = global::UnityEditor.AssetDatabase
                .LoadAssetAtPath<global::UnityEditor.Animations.AnimatorController>(target);
        }}

        if (controller != null)
        {{
            foreach (var parameter in controller.parameters)
            {{
                parameters.Add(parameter.name + ":" + parameter.type);
            }}
            foreach (var layer in controller.layers)
            {{
                foreach (var child in layer.stateMachine.states)
                {{
                    string motion = child.state.motion == null ? "-" : child.state.motion.name;
                    states.Add(layer.name + "/" + child.state.name + ":" + motion);
                }}
            }}
            foreach (var clip in controller.animationClips)
            {{
                var binding = global::UnityEditor.EditorCurveBinding.PPtrCurve(
                    "", typeof(global::UnityEngine.SpriteRenderer), "m_Sprite");
                var keys = global::UnityEditor.AnimationUtility
                    .GetObjectReferenceCurve(clip, binding);
                int frames = keys == null ? 0 : keys.Length;
                clips.Add(clip.name + ":" + frames + ":" + clip.length);
            }}
        }}

        string payload = "{{\\"success\\":" + (controller != null ? "true" : "false")
            + ",\\"target\\":\\"" + target + "\\""
            + ",\\"controller\\":\\"" + controllerPath + "\\""
            + ",\\"hasAnimator\\":" + (hasAnimator ? "true" : "false")
            + ",\\"states\\":" + JsonArray(states)
            + ",\\"parameters\\":" + JsonArray(parameters)
            + ",\\"clips\\":" + JsonArray(clips)
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


# ---------------------------------------------------------------------------
# run_named_tests
# ---------------------------------------------------------------------------
#: Test modes ``TestRunnerApi`` accepts.
TEST_MODES = ("EditMode", "PlayMode")

#: Keys the editor-side reporter writes. Kept here so the poll command and the
#: template cannot drift apart silently.
TEST_STATUS_KEY = "pipeline.tests.status"
TEST_RESULTS_KEY = "pipeline.tests.results"
TEST_COUNT_KEY = "pipeline.tests.count"


def test_start_command(mode: str, test_names: list[str]) -> str:
    """Start a filtered test run and leave the waiting to the poll command.

    A test run crosses a domain reload, so nothing registered here survives to see
    the result. This only starts the run; ``PipelineTestReporter`` in the target
    project records what happens (see ``docs/guide-boards.md``).
    """

    body = f"""        string[] names = new string[] {{ {_string_array(test_names)} }};

        global::UnityEditor.EditorPrefs.SetString({_literal(TEST_STATUS_KEY)}, "starting");
        global::UnityEditor.EditorPrefs.SetString({_literal(TEST_RESULTS_KEY)}, "");
        global::UnityEditor.EditorPrefs.SetInt({_literal(TEST_COUNT_KEY)}, 0);

        var api = global::UnityEngine.ScriptableObject
            .CreateInstance<global::UnityEditor.TestTools.TestRunner.Api.TestRunnerApi>();
        var filter = new global::UnityEditor.TestTools.TestRunner.Api.Filter();
        filter.testMode = global::UnityEditor.TestTools.TestRunner.Api.TestMode.{mode};
        if (names.Length > 0)
        {{
            filter.testNames = names;
        }}

        api.Execute(new global::UnityEditor.TestTools.TestRunner.Api.ExecutionSettings(filter));

        string payload = "{{\\"success\\":true"
            + ",\\"mode\\":\\"{mode}\\""
            + ",\\"requested\\":" + names.Length
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)


def test_poll_command() -> str:
    """Read whatever the reporter has written so far.

    ``status`` comes back as ``absent`` when the reporter was never installed, which
    is a different problem from a run that is still going.
    """

    body = f"""        string status = global::UnityEditor.EditorPrefs
            .GetString({_literal(TEST_STATUS_KEY)}, "absent");
        string results = global::UnityEditor.EditorPrefs
            .GetString({_literal(TEST_RESULTS_KEY)}, "");
        int count = global::UnityEditor.EditorPrefs.GetInt({_literal(TEST_COUNT_KEY)}, 0);

        string payload = "{{\\"success\\":true"
            + ",\\"status\\":\\"" + status + "\\""
            + ",\\"count\\":" + count
            + ",\\"results\\":" + (results.Length > 0 ? results : "[]")
            + "}}";
        result.Log("{RESULT_MARKER} {{0}}", payload);"""
    return _wrap(body)
