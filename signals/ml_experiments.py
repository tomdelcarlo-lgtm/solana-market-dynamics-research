# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""Historical executed ML cell bodies (no saved outputs, token data or models).
These functions preserve the exploratory protocol, including test-set reuse.
Optional ML dependencies are imported only inside the relevant callable.
This is not a repaired validation protocol or a claim of new model performance.
"""

def random_split(X, y):
    from sklearn.model_selection import train_test_split
    #Let's split the dataset into training (80%) and testing (20%) sets

    from sklearn.model_selection import train_test_split

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.20,
        random_state=1,
        stratify=y
    )

    print("Training set size:", X_train.shape)
    print("Test set size:", X_test.shape)

    #Let's confirm graduated ratio is preserved
    print("\nGraduated ratio in training set:")
    print((y_train.value_counts(normalize=True) * 100).round(2))

    print("\nGraduated ratio in test set:")
    print((y_test.value_counts(normalize=True) * 100).round(2))
    return X_train, X_test, y_train, y_test

#Let's define a function to evaluate classification performance for imbalanced datasets


def model_performance_classification(model, predictors, target):
    import pandas as pd
    from sklearn.metrics import recall_score, precision_score, f1_score, roc_auc_score, average_precision_score
    """
    Function to compute classification metrics appropriate for imbalanced datasets.

    model: trained classifier
    predictors: features (X_test)
    target: true labels (y_test)
    """

    # predicted class labels
    pred = model.predict(predictors)

    # predicted probabilities (required for AUC metrics)
    pred_proba = model.predict_proba(predictors)[:,1]

    # Recall
    # Measures how many tokens that actually graduated were correctly identified
    recall = recall_score(target, pred)

    # Precision
    # Measures how many predicted graduations are actually correct
    precision = precision_score(target, pred)

    # F1 Score
    # Harmonic mean of precision and recall (balanced metric for imbalanced datasets)
    f1 = f1_score(target, pred)

    # ROC-AUC
    # Measures the model's ability to separate the two classes across thresholds
    roc_auc = roc_auc_score(target, pred_proba)

    # PR-AUC
    # Most informative metric when the positive class is rare
    pr_auc = average_precision_score(target, pred_proba)

    # create dataframe
    df_perf = pd.DataFrame({
        "Recall": recall,
        "Precision": precision,
        "F1 Score": f1,
        "ROC-AUC": roc_auc,
        "PR-AUC": pr_auc
    }, index=[0])

    return df_perf

def smote_training(X_train, y_train):
    from imblearn.over_sampling import SMOTE
    # Let's balance the training data using SMOTE (only on the training set)
    from imblearn.over_sampling import SMOTE

    # Initialize SMOTE
    smote = SMOTE(random_state=1)

    # Apply SMOTE to training data
    X_train_bal, y_train_bal = smote.fit_resample(X_train, y_train)

    # Verify class balance
    print("Class distribution BEFORE SMOTE:")
    print(y_train.value_counts(normalize=True).round(3))

    print("\nClass distribution AFTER SMOTE:")
    print(pd.Series(y_train_bal).value_counts(normalize=True).round(3))
    return X_train_bal, y_train_bal

def xgboost_search(X_train_bal, y_train_bal):
    from xgboost import XGBClassifier
    from sklearn.model_selection import RandomizedSearchCV
    #Let's perform Hyperparameter Tuning for XGBoost

    from sklearn.model_selection import RandomizedSearchCV
    from xgboost import XGBClassifier

    # Base model
    xgb = XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=1,
        n_jobs=-1
    )

    # Hyperparameter search space
    param_dist = {

        "n_estimators": [200, 300, 400, 500],

        "max_depth": [3,4,5,6,7],

        "learning_rate": [0.01, 0.03, 0.05, 0.1],

        "subsample": [0.7,0.8,0.9,1.0],

        "colsample_bytree": [0.7,0.8,0.9,1.0],

        "min_child_weight": [1,3,5,7],

        "gamma": [0,0.1,0.2,0.3]
    }

    # Randomized search
    random_search = RandomizedSearchCV(
        estimator=xgb,
        param_distributions=param_dist,
        n_iter=40,
        scoring="average_precision",  # PR-AUC
        cv=3,
        verbose=1,
        random_state=1,
        n_jobs=-1
    )

    # Fit on SMOTE balanced data
    random_search.fit(X_train_bal, y_train_bal)

    # Best model
    best_model = random_search.best_estimator_

    print("Best Parameters:")
    print(random_search.best_params_)

    return random_search

def isotonic_calibration(best_model, X_train_bal, y_train_bal):
    from sklearn.calibration import CalibratedClassifierCV
    #Let's calibrate the tuned XGBoost probabilities

    from sklearn.calibration import CalibratedClassifierCV

    # Calibrate probabilities
    calibrated_model = CalibratedClassifierCV(
        best_model,
        method="isotonic",
        cv=3
    )

    calibrated_model.fit(X_train_bal, y_train_bal)

    print("Model calibration completed")

    return calibrated_model

def shap_explanation(best_model, X_test):
    import shap
    #Let's import SHAP and create the explainer

    import shap

    #Use the trained Gradient Boosting model
    explainer = shap.TreeExplainer(best_model)

    #Compute SHAP values on the test set
    shap_values = explainer.shap_values(X_test)
    return shap_values


def baseline_models(X_train, y_train, X_test, y_test):
    import pandas as pd
    #Let's import the required models

    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (
        BaggingClassifier,
        RandomForestClassifier,
        GradientBoostingClassifier,
        AdaBoostClassifier
    )
    from sklearn.tree import DecisionTreeClassifier

    from xgboost import XGBClassifier

    #List of models
    models = []

    #Baseline models
    models.append(("Logistic Regression",
                   LogisticRegression(max_iter=1000, class_weight="balanced")))

    models.append(("Decision Tree",
                   DecisionTreeClassifier(random_state=1, class_weight="balanced")))

    #Ensemble models
    models.append(("Bagging",
                   BaggingClassifier(
                       estimator=DecisionTreeClassifier(random_state=1, class_weight="balanced"),
                       random_state=1)))

    models.append(("Random Forest",
                   RandomForestClassifier(random_state=1, class_weight="balanced")))

    models.append(("Gradient Boosting",
                   GradientBoostingClassifier(random_state=1)))

    models.append(("AdaBoost",
                   AdaBoostClassifier(random_state=1)))

    #Strong gradient boosting model
    models.append(("XGBoost",
                   XGBClassifier(
                       random_state=1,
                       scale_pos_weight= (len(y_train[y_train==0]) / len(y_train[y_train==1])),
                       eval_metric="logloss"
                   )))

    #Evaluate models
    print("\nModel Performance (Test Set):\n")

    results = []

    for name, model in models:

        model.fit(X_train, y_train)

        perf = model_performance_classification(
            model,
            X_test,
            y_test
        )

        perf.insert(0, "Model", name)

        results.append(perf)

    results_df = pd.concat(results, ignore_index=True)

    print(results_df)
    return models, results_df


def smote_model_comparison(models, X_train_bal, y_train_bal, X_test, y_test):
    import pandas as pd
    #Let's re-train the same models using SMOTE-balanced training data
    from sklearn.metrics import recall_score, precision_score, f1_score, roc_auc_score, average_precision_score
    import pandas as pd

    results = []

    print("\nModel Performance (Test Set — SMOTE balanced training):\n")

    for name, model in models:

        # Train model on balanced training data
        model.fit(X_train_bal, y_train_bal)

        # Predictions
        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:,1] if hasattr(model, "predict_proba") else None

        # Metrics
        recall = recall_score(y_test, y_pred)
        precision = precision_score(y_test, y_pred)
        f1 = f1_score(y_test, y_pred)

        roc_auc = roc_auc_score(y_test, y_prob) if y_prob is not None else None
        pr_auc = average_precision_score(y_test, y_prob) if y_prob is not None else None

        results.append({
            "Model": name,
            "Recall": recall,
            "Precision": precision,
            "F1 Score": f1,
            "ROC-AUC": roc_auc,
            "PR-AUC": pr_auc
        })

    results_df = pd.DataFrame(results).sort_values("PR-AUC", ascending=False)

    print(results_df)
    return results_df


def threshold_sweep(y_probs, y_test):
    #Let's evaluate multiple classification thresholds

    import numpy as np
    import pandas as pd
    from sklearn.metrics import recall_score, precision_score, f1_score

    thresholds = np.linspace(0.01,0.99,99)

    metrics = []

    for t in thresholds:

        y_pred_t = (y_probs >= t).astype(int)

        recall = recall_score(y_test, y_pred_t)
        precision = precision_score(y_test, y_pred_t)
        f1 = f1_score(y_test, y_pred_t)

        metrics.append({
            "Threshold": t,
            "Recall": recall,
            "Precision": precision,
            "F1": f1
        })

    threshold_df = pd.DataFrame(metrics)

    threshold_df.sort_values("F1", ascending=False).head(10)
    return threshold_df
