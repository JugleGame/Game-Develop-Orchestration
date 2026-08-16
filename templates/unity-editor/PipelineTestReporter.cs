// Copy this file and PipelineTestReporter.asmdef to <Unity project>/Assets/Editor/.
// Requires the Test Framework package, which a standard Unity 6 project already has.
//
// Why a file in the project rather than code the MCP tool injects: a test run crosses a
// domain reload, and anything the injected command registered dies with it. An
// [InitializeOnLoad] class re-registers itself after every reload, so the results of a
// PlayMode run survive to be read back. EditorPrefs is the hand-off because the MCP
// command runner refuses source that so much as mentions File.WriteAllText.

using System.Text;
using UnityEditor;
using UnityEditor.TestTools.TestRunner.Api;
using UnityEngine;

[InitializeOnLoad]
public static class PipelineTestReporter
{
    public const string StatusKey = "pipeline.tests.status";
    public const string ResultsKey = "pipeline.tests.results";
    public const string CountKey = "pipeline.tests.count";

    private static readonly TestRunnerApi Api;
    private static readonly StringBuilder Collected = new StringBuilder();
    private static int collectedCount;

    static PipelineTestReporter()
    {
        Api = ScriptableObject.CreateInstance<TestRunnerApi>();
        Api.RegisterCallbacks(new Callbacks());
    }

    /// <summary>Clear the previous run so a poll cannot read a stale result.</summary>
    public static void Reset()
    {
        Collected.Length = 0;
        collectedCount = 0;
        EditorPrefs.SetString(StatusKey, "starting");
        EditorPrefs.SetString(ResultsKey, "");
        EditorPrefs.SetInt(CountKey, 0);
    }

    private static string Escape(string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return "";
        }

        return value
            .Replace("\\", "/")
            .Replace("\"", "'")
            .Replace("\r", " ")
            .Replace("\n", " ");
    }

    private class Callbacks : ICallbacks
    {
        public void RunStarted(ITestAdaptor testsToRun)
        {
            Collected.Length = 0;
            collectedCount = 0;
            EditorPrefs.SetString(StatusKey, "running");
        }

        public void TestStarted(ITestAdaptor test)
        {
        }

        public void TestFinished(ITestResultAdaptor result)
        {
            // Only leaves are individual tests; the parents are suites.
            if (result.Test.IsSuite)
            {
                return;
            }

            if (collectedCount > 0)
            {
                Collected.Append(",");
            }

            Collected.Append("{\"name\":\"").Append(Escape(result.Test.Name))
                .Append("\",\"fullName\":\"").Append(Escape(result.Test.FullName))
                .Append("\",\"status\":\"").Append(result.TestStatus)
                .Append("\",\"durationSeconds\":").Append(result.Duration.ToString("F3"))
                .Append(",\"message\":\"").Append(Escape(result.Message))
                .Append("\"}");
            collectedCount++;

            // Written every test, not only at the end: a run that never finishes still
            // leaves behind what it managed to prove.
            EditorPrefs.SetString(ResultsKey, "[" + Collected + "]");
            EditorPrefs.SetInt(CountKey, collectedCount);
        }

        public void RunFinished(ITestResultAdaptor result)
        {
            EditorPrefs.SetString(ResultsKey, "[" + Collected + "]");
            EditorPrefs.SetInt(CountKey, collectedCount);
            EditorPrefs.SetString(StatusKey, "completed");
        }
    }
}
