// Copy this file to <Unity project>/Assets/Editor/SpriteAnimationCreatorWindow.cs.
// Requires Unity's 2D Sprite package, included in a standard Unity 6 2D project.

using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.U2D.Sprites;
using UnityEngine;

public sealed class SpriteAnimationCreatorWindow : EditorWindow
{
    private const string DefaultOutputFolder = "Assets/Animations";

    [SerializeField] private Texture2D sourceTexture;
    [SerializeField] private int columns = 4;
    [SerializeField] private int rows = 1;
    [SerializeField] private float framesPerSecond = 12f;
    [SerializeField] private bool loop = true;
    [SerializeField] private string clipName = "NewSpriteAnimation";
    [SerializeField] private string outputFolder = DefaultOutputFolder;

    [MenuItem("Tools/Game Development/Sprite Animation Creator")]
    private static void Open()
    {
        GetWindow<SpriteAnimationCreatorWindow>("Sprite Animation Creator");
    }

    private void OnGUI()
    {
        EditorGUILayout.LabelField("Sprite Sheet", EditorStyles.boldLabel);
        sourceTexture = (Texture2D)EditorGUILayout.ObjectField(
            "Source Texture", sourceTexture, typeof(Texture2D), false);
        columns = EditorGUILayout.IntField("Columns", columns);
        rows = EditorGUILayout.IntField("Rows", rows);

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("Animation Clip", EditorStyles.boldLabel);
        clipName = EditorGUILayout.TextField("Clip Name", clipName);
        framesPerSecond = EditorGUILayout.FloatField("Frames Per Second", framesPerSecond);
        loop = EditorGUILayout.Toggle("Loop", loop);
        outputFolder = EditorGUILayout.TextField("Output Folder", outputFolder);

        EditorGUILayout.Space();
        using (new EditorGUI.DisabledScope(!CanCreate(out _)))
        {
            if (GUILayout.Button("Slice Sheet and Create Animation"))
            {
                CreateAnimation();
            }
        }

        if (!CanCreate(out var validationMessage))
        {
            EditorGUILayout.HelpBox(validationMessage, MessageType.Info);
        }
    }

    private bool CanCreate(out string message)
    {
        if (sourceTexture == null)
        {
            message = "Select a texture asset from this Unity project.";
            return false;
        }

        if (columns < 1 || rows < 1)
        {
            message = "Columns and rows must both be at least 1.";
            return false;
        }

        if (sourceTexture.width % columns != 0 || sourceTexture.height % rows != 0)
        {
            message = "Texture width and height must divide evenly by the selected grid.";
            return false;
        }

        if (framesPerSecond <= 0f)
        {
            message = "Frames per second must be greater than zero.";
            return false;
        }

        if (string.IsNullOrWhiteSpace(clipName))
        {
            message = "Enter an animation clip name.";
            return false;
        }

        if (clipName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || clipName.Contains("/") || clipName.Contains("\\"))
        {
            message = "Clip Name must be a file name, not a path.";
            return false;
        }

        if (string.IsNullOrWhiteSpace(outputFolder) || !outputFolder.StartsWith("Assets/", StringComparison.Ordinal)
            || outputFolder.Split('/').Any(segment => string.IsNullOrWhiteSpace(segment) || segment == "." || segment == ".."))
        {
            message = "Output Folder must be inside this project and start with Assets/.";
            return false;
        }

        var sourcePath = AssetDatabase.GetAssetPath(sourceTexture);
        if (string.IsNullOrEmpty(sourcePath) || !sourcePath.StartsWith("Assets/", StringComparison.Ordinal))
        {
            message = "The source texture must be an asset inside this Unity project.";
            return false;
        }

        message = string.Empty;
        return true;
    }

    private void CreateAnimation()
    {
        if (!CanCreate(out var validationMessage))
        {
            EditorUtility.DisplayDialog("Cannot create animation", validationMessage, "OK");
            return;
        }

        var texturePath = AssetDatabase.GetAssetPath(sourceTexture);
        var importer = AssetImporter.GetAtPath(texturePath) as TextureImporter;
        if (importer == null)
        {
            EditorUtility.DisplayDialog("Cannot create animation", "Texture importer was not found.", "OK");
            return;
        }

        SliceTexture(importer);
        var sprites = AssetDatabase.LoadAllAssetsAtPath(texturePath)
            .OfType<Sprite>()
            .OrderBy(sprite => sprite.name, StringComparer.Ordinal)
            .ToArray();
        if (sprites.Length != columns * rows)
        {
            EditorUtility.DisplayDialog(
                "Cannot create animation",
                $"Expected {columns * rows} sprites after slicing, but Unity imported {sprites.Length}.",
                "OK");
            return;
        }

        EnsureOutputFolder();
        var clip = CreateClip(sprites);
        var path = AssetDatabase.GenerateUniqueAssetPath($"{outputFolder}/{clipName}.anim");
        AssetDatabase.CreateAsset(clip, path);
        AssetDatabase.SaveAssets();
        Selection.activeObject = clip;
        EditorGUIUtility.PingObject(clip);
        EditorUtility.DisplayDialog("Sprite animation created", $"Created {path}", "OK");
    }

    private void SliceTexture(TextureImporter importer)
    {
        importer.textureType = TextureImporterType.Sprite;
        importer.spriteImportMode = SpriteImportMode.Multiple;
        importer.filterMode = FilterMode.Point;
        importer.mipmapEnabled = false;
        importer.SaveAndReimport();

        var factory = new SpriteDataProviderFactories();
        factory.Init();
        var provider = factory.GetSpriteEditorDataProviderFromObject(importer);
        if (provider == null)
        {
            throw new InvalidOperationException("Unity could not create a sprite data provider for this texture.");
        }

        provider.InitSpriteEditorDataProvider();
        var frameWidth = sourceTexture.width / columns;
        var frameHeight = sourceTexture.height / rows;
        var rects = new SpriteRect[columns * rows];
        var index = 0;
        for (var row = 0; row < rows; row++)
        {
            for (var column = 0; column < columns; column++)
            {
                rects[index] = new SpriteRect
                {
                    name = $"{sourceTexture.name}_{index:D3}",
                    rect = new Rect(column * frameWidth, (rows - 1 - row) * frameHeight, frameWidth, frameHeight),
                    pivot = new Vector2(0.5f, 0.5f),
                    alignment = SpriteAlignment.Center,
                    spriteID = GUID.Generate()
                };
                index++;
            }
        }

        provider.SetSpriteRects(rects);
        var nameProvider = provider.GetDataProvider<ISpriteNameFileIdDataProvider>();
        if (nameProvider != null)
        {
            nameProvider.SetNameFileIdPair(rects.Select(rect => new SpriteNameFileIdPair(rect.name, rect.spriteID)).ToList());
        }
        provider.Apply();
        importer.SaveAndReimport();
    }

    private AnimationClip CreateClip(Sprite[] sprites)
    {
        var clip = new AnimationClip { frameRate = framesPerSecond, wrapMode = loop ? WrapMode.Loop : WrapMode.Default };
        var binding = EditorCurveBinding.PPtrCurve(string.Empty, typeof(SpriteRenderer), "m_Sprite");
        var keys = sprites.Select((sprite, index) => new ObjectReferenceKeyframe
        {
            time = index / framesPerSecond,
            value = sprite
        }).ToArray();
        AnimationUtility.SetObjectReferenceCurve(clip, binding, keys);
        return clip;
    }

    private void EnsureOutputFolder()
    {
        var folders = outputFolder.Split('/');
        var current = folders[0];
        for (var index = 1; index < folders.Length; index++)
        {
            var next = $"{current}/{folders[index]}";
            if (!AssetDatabase.IsValidFolder(next))
            {
                AssetDatabase.CreateFolder(current, folders[index]);
            }
            current = next;
        }
    }
}
