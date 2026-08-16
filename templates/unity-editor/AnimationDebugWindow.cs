// Copy this file to <Unity project>/Assets/Editor/AnimationDebugWindow.cs.
// Companion to SpriteAnimationCreatorWindow.cs: that one slices one sheet, this one
// builds a clip from loose frame files and shows why an Animator is doing nothing.
//
// Three things a human needs and the MCP tools cannot give them:
//   1. Build a clip from the frame PNGs an asset pipeline dropped in a folder.
//   2. See, in one list, which controllers exist and what states/parameters they carry.
//   3. Poke a live Animator's parameters during PlayMode and watch the transition fire.

using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;

public sealed class AnimationDebugWindow : EditorWindow
{
    private const string DefaultOutputFolder = "Assets/Animations";

    [SerializeField] private DefaultAsset frameFolder;
    [SerializeField] private string clipName = "NewClip";
    [SerializeField] private float framesPerSecond = 12f;
    [SerializeField] private bool loop = true;
    [SerializeField] private string outputFolder = DefaultOutputFolder;

    private Vector2 scroll;
    private Animator inspected;
    private readonly List<string> report = new List<string>();

    [MenuItem("Tools/Game Development/Animation Debugger")]
    private static void Open()
    {
        GetWindow<AnimationDebugWindow>("Animation Debugger");
    }

    private void OnGUI()
    {
        scroll = EditorGUILayout.BeginScrollView(scroll);

        DrawClipBuilder();
        EditorGUILayout.Space();
        DrawControllerReport();
        EditorGUILayout.Space();
        DrawLiveParameters();

        EditorGUILayout.EndScrollView();
    }

    // -- 1. frames -> clip ---------------------------------------------------

    private void DrawClipBuilder()
    {
        EditorGUILayout.LabelField("Clip From Frame Folder", EditorStyles.boldLabel);
        frameFolder = (DefaultAsset)EditorGUILayout.ObjectField(
            "Frame Folder", frameFolder, typeof(DefaultAsset), false);
        clipName = EditorGUILayout.TextField("Clip Name", clipName);
        framesPerSecond = EditorGUILayout.FloatField("Frames Per Second", framesPerSecond);
        loop = EditorGUILayout.Toggle("Loop", loop);
        outputFolder = EditorGUILayout.TextField("Output Folder", outputFolder);

        string folderPath = frameFolder == null ? "" : AssetDatabase.GetAssetPath(frameFolder);
        bool folderIsValid = folderPath.Length > 0 && AssetDatabase.IsValidFolder(folderPath);

        using (new EditorGUI.DisabledScope(!folderIsValid || framesPerSecond <= 0f))
        {
            if (GUILayout.Button("Build Clip From Folder"))
            {
                BuildClip(folderPath);
            }
        }

        if (!folderIsValid)
        {
            EditorGUILayout.HelpBox(
                "Pick a project folder that holds the frame PNGs. Frames play in file-name order.",
                MessageType.Info);
        }
    }

    private void BuildClip(string folderPath)
    {
        // File-name order is the play order: the generator zero-pads the index for exactly this.
        var sprites = AssetDatabase.FindAssets("t:Sprite", new[] { folderPath })
            .Select(AssetDatabase.GUIDToAssetPath)
            .OrderBy(path => path, System.StringComparer.Ordinal)
            .Select(AssetDatabase.LoadAssetAtPath<Sprite>)
            .Where(sprite => sprite != null)
            .ToList();

        if (sprites.Count == 0)
        {
            EditorUtility.DisplayDialog("Animation Debugger", "That folder holds no sprites.", "OK");
            return;
        }

        if (!AssetDatabase.IsValidFolder(outputFolder))
        {
            EditorUtility.DisplayDialog(
                "Animation Debugger", $"Output folder does not exist: {outputFolder}", "OK");
            return;
        }

        var clip = new AnimationClip { frameRate = framesPerSecond };
        var binding = EditorCurveBinding.PPtrCurve("", typeof(SpriteRenderer), "m_Sprite");
        var keys = new ObjectReferenceKeyframe[sprites.Count];
        for (int i = 0; i < sprites.Count; i++)
        {
            keys[i] = new ObjectReferenceKeyframe
            {
                time = i / framesPerSecond,
                value = sprites[i],
            };
        }

        AnimationUtility.SetObjectReferenceCurve(clip, binding, keys);
        var settings = AnimationUtility.GetAnimationClipSettings(clip);
        settings.loopTime = loop;
        AnimationUtility.SetAnimationClipSettings(clip, settings);

        string clipPath = $"{outputFolder.TrimEnd('/')}/{clipName}.anim";
        AssetDatabase.CreateAsset(clip, AssetDatabase.GenerateUniqueAssetPath(clipPath));
        AssetDatabase.SaveAssets();
        Selection.activeObject = clip;

        Debug.Log($"[Animation Debugger] built {clipPath} from {sprites.Count} frames.");
    }

    // -- 2. what is actually in the project ---------------------------------

    private void DrawControllerReport()
    {
        EditorGUILayout.LabelField("Controllers And Silent Animators", EditorStyles.boldLabel);

        if (GUILayout.Button("Scan Project"))
        {
            Scan();
        }

        foreach (var line in report)
        {
            EditorGUILayout.LabelField(line, EditorStyles.wordWrappedLabel);
        }
    }

    private void Scan()
    {
        report.Clear();

        foreach (var guid in AssetDatabase.FindAssets("t:AnimatorController"))
        {
            string path = AssetDatabase.GUIDToAssetPath(guid);
            var controller = AssetDatabase.LoadAssetAtPath<AnimatorController>(path);
            if (controller == null)
            {
                continue;
            }

            var states = controller.layers
                .SelectMany(layer => layer.stateMachine.states)
                .Select(child => child.state.name + (child.state.motion == null ? " (no clip)" : ""));
            var parameters = controller.parameters.Select(item => $"{item.name}:{item.type}");
            var frames = controller.animationClips.Select(clip =>
            {
                var binding = EditorCurveBinding.PPtrCurve("", typeof(SpriteRenderer), "m_Sprite");
                var keys = AnimationUtility.GetObjectReferenceCurve(clip, binding);
                return $"{clip.name}={(keys == null ? 0 : keys.Length)}f";
            });

            report.Add($"{path}\n  states: {string.Join(", ", states)}"
                + $"\n  parameters: {string.Join(", ", parameters)}"
                + $"\n  clip frames: {string.Join(", ", frames)}");
        }

        // The failure this window exists for: an Animator that silently does nothing.
        foreach (var guid in AssetDatabase.FindAssets("t:Prefab"))
        {
            string path = AssetDatabase.GUIDToAssetPath(guid);
            var root = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (root == null)
            {
                continue;
            }

            foreach (var animator in root.GetComponentsInChildren<Animator>(true))
            {
                if (animator.runtimeAnimatorController == null)
                {
                    report.Add($"WARNING {path}: Animator on '{animator.name}' has no controller. "
                        + "Parameter calls from game code do nothing.");
                }
            }
        }

        foreach (var animator in Object.FindObjectsByType<Animator>(FindObjectsInactive.Include))
        {
            if (animator.runtimeAnimatorController == null)
            {
                report.Add($"WARNING scene object '{animator.name}': Animator has no controller.");
            }
        }

        if (report.Count == 0)
        {
            report.Add("No AnimatorController assets found in this project.");
        }
    }

    // -- 3. poke a live Animator --------------------------------------------

    private void DrawLiveParameters()
    {
        EditorGUILayout.LabelField("Live Parameters (PlayMode)", EditorStyles.boldLabel);
        inspected = (Animator)EditorGUILayout.ObjectField("Animator", inspected, typeof(Animator), true);

        if (!Application.isPlaying)
        {
            EditorGUILayout.HelpBox("Enter PlayMode to drive parameters.", MessageType.Info);
            return;
        }

        if (inspected == null || inspected.runtimeAnimatorController == null)
        {
            EditorGUILayout.HelpBox(
                "Assign a scene Animator that has a controller.", MessageType.Warning);
            return;
        }

        foreach (var parameter in inspected.parameters)
        {
            switch (parameter.type)
            {
                case AnimatorControllerParameterType.Float:
                    float floatValue = EditorGUILayout.FloatField(
                        parameter.name, inspected.GetFloat(parameter.name));
                    inspected.SetFloat(parameter.name, floatValue);
                    break;

                case AnimatorControllerParameterType.Int:
                    int intValue = EditorGUILayout.IntField(
                        parameter.name, inspected.GetInteger(parameter.name));
                    inspected.SetInteger(parameter.name, intValue);
                    break;

                case AnimatorControllerParameterType.Bool:
                    bool boolValue = EditorGUILayout.Toggle(
                        parameter.name, inspected.GetBool(parameter.name));
                    inspected.SetBool(parameter.name, boolValue);
                    break;

                case AnimatorControllerParameterType.Trigger:
                    if (GUILayout.Button($"Fire {parameter.name}"))
                    {
                        inspected.SetTrigger(parameter.name);
                    }

                    break;
            }
        }

        var current = inspected.GetCurrentAnimatorStateInfo(0);
        EditorGUILayout.LabelField("Current state hash", current.fullPathHash.ToString());
        EditorGUILayout.LabelField("Normalized time", current.normalizedTime.ToString("F2"));
        Repaint();
    }
}
