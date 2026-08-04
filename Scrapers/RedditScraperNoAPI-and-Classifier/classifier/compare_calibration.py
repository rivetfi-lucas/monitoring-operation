"""
Compares two classify_comments.py outputs (e.g. Sonnet vs Haiku on the same
calibration sample) so you can decide which model to run the full 40k on.

Usage:
    python compare_calibration.py \\
        output/reddit_comments_20260718_classified_sonnet.csv \\
        output/reddit_comments_20260718_classified_haiku.csv

Output:
    Prints agreement stats to console.
    Writes calibration_comparison.csv with every comment where the two models
    disagreed on icp_likelihood or relevance_tag, for manual review.
"""
import argparse
import os

import pandas as pd


def main(path_a, path_b, label_a="sonnet", label_b="haiku"):
    df_a = pd.read_csv(path_a, dtype=str)
    df_b = pd.read_csv(path_b, dtype=str)

    merged = df_a.merge(
        df_b[["comment_id", "relevance_tag", "icp_likelihood", "confidence", "rationale"]],
        on="comment_id",
        suffixes=(f"_{label_a}", f"_{label_b}"),
        how="inner",
    )
    if len(merged) < min(len(df_a), len(df_b)):
        print(f"[warn] only {len(merged)} comment_ids matched between the two files "
              f"({len(df_a)} in {label_a}, {len(df_b)} in {label_b}) — "
              f"make sure both were run with the same --sample and --seed.")

    n = len(merged)
    icp_agree = (merged[f"icp_likelihood_{label_a}"] == merged[f"icp_likelihood_{label_b}"]).sum()
    tag_agree = (merged[f"relevance_tag_{label_a}"] == merged[f"relevance_tag_{label_b}"]).sum()

    print(f"\nCompared {n} comments classified by both models.\n")
    print(f"icp_likelihood agreement:   {icp_agree}/{n} ({100 * icp_agree / n:.1f}%)")
    print(f"relevance_tag agreement:    {tag_agree}/{n} ({100 * tag_agree / n:.1f}%)")

    print(f"\nConfidence distribution — {label_a}:")
    print(merged[f"confidence_{label_a}"].value_counts().to_string())
    print(f"\nConfidence distribution — {label_b}:")
    print(merged[f"confidence_{label_b}"].value_counts().to_string())

    disagreements = merged[
        (merged[f"icp_likelihood_{label_a}"] != merged[f"icp_likelihood_{label_b}"])
        | (merged[f"relevance_tag_{label_a}"] != merged[f"relevance_tag_{label_b}"])
    ]
    print(f"\n{len(disagreements)} comments where the models disagreed on tag or ICP — "
          f"these are the ones worth reading by hand.")

    out_path = "calibration_comparison.csv"
    disagreements.to_csv(out_path, index=False)
    print(f"Written to: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("classified_csv_a", help="e.g. output/x_classified_sonnet.csv")
    parser.add_argument("classified_csv_b", help="e.g. output/x_classified_haiku.csv")
    args = parser.parse_args()

    def label_from_path(p):
        stem = os.path.splitext(os.path.basename(p))[0]
        return stem.split("_")[-1]

    main(
        args.classified_csv_a,
        args.classified_csv_b,
        label_from_path(args.classified_csv_a),
        label_from_path(args.classified_csv_b),
    )
