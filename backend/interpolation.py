"""
Spectral interpolation, ported verbatim from ChemDrawAutomationScripts/interpolation.py.

Five methods (cubic spline, Akima spline, linear, RBF, GMM) plus the
"generated points only" verification technique documented in that repo's
VERIFICATION_TECHNIQUE.md. The algorithms are unchanged; only the unused
matplotlib import was removed, since plotting happens in the browser.
"""
import numpy as np
import pandas as pd
from scipy.interpolate import CubicSpline, Akima1DInterpolator, interp1d, Rbf
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

def validate_interpolation_data(x, y, compound_id):
    """
    Validate input data for interpolation.
    
    Parameters:
    - x: wavelength values
    - y: intensity values
    - compound_id: compound identifier
    
    Returns:
    - bool: True if data is valid, False otherwise
    """
    try:
        x = np.array(x)
        y = np.array(y)
        
        # Check for valid data types
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            print(f"Warning: Non-finite values found in {compound_id}")
            return False
        
        # Check for sufficient data points
        if len(x) < 2:
            print(f"Warning: Insufficient data points for {compound_id} (need at least 2)")
            return False
        
        # Check for unique x values
        if len(np.unique(x)) != len(x):
            print(f"Warning: Duplicate x values found in {compound_id}")
            return False
        
        # Check for reasonable range
        if np.max(x) - np.min(x) < 1e-6:
            print(f"Warning: Very small wavelength range for {compound_id}")
            return False
        
        return True
        
    except Exception as e:
        print(f"Error validating data for {compound_id}: {e}")
        return False

def interpolate_methods_fixed(x, y, compound_id, step=1.0):
    """
    Interpolates spectral data using multiple methods, with corrected GMM.
    
    Parameters:
    - x: list or np.array of wavelengths (nm)
    - y: list or np.array of intensities
    - compound_id: identifier for the compound
    - step: step size for interpolation (e.g., 1.0 or 0.5 nm)
    
    Returns: DataFrame with interpolated results for all methods
    """
    # Validate input data
    if not validate_interpolation_data(x, y, compound_id):
        raise ValueError(f"Invalid data for compound {compound_id}")
    
    x = np.array(x)
    y = np.array(y)
    
    # Ensure we don't exceed the maximum value of original data
    x_min, x_max = min(x), max(x)
    x_new = np.arange(x_min, x_max + step/2, step)  # Add small buffer to include max value
    # Clip to ensure we don't exceed the original range
    x_new = x_new[x_new <= x_max]
    
    results = {'wavelength': x_new}
    
    # Cubic Spline
    try:
        cs = CubicSpline(x, y)
        results['cubic_spline'] = cs(x_new)
    except Exception as e:
        print(f"Warning: Cubic spline failed for {compound_id}: {e}")
        results['cubic_spline'] = np.interp(x_new, x, y)
    
    # Akima Spline
    try:
        # Debug: Check input data for Akima spline
        print(f"Debug: Akima spline input for {compound_id}")
        print(f"  x range: {np.min(x)} to {np.max(x)}")
        print(f"  y range: {np.min(y)} to {np.max(y)}")
        print(f"  x unique: {len(np.unique(x))}")
        print(f"  y finite: {np.isfinite(y).all()}")
        
        akima = Akima1DInterpolator(x, y)
        akima_result = akima(x_new)
        
        # Debug: Check Akima result
        if np.any(np.isnan(akima_result)):
            print(f"Warning: NaN values in Akima spline result for {compound_id}")
            print(f"  Result range: {np.nanmin(akima_result)} to {np.nanmax(akima_result)}")
            # Fallback to linear interpolation
            results['akima_spline'] = np.interp(x_new, x, y)
        else:
            results['akima_spline'] = akima_result
    except Exception as e:
        print(f"Warning: Akima spline failed for {compound_id}: {e}")
        results['akima_spline'] = np.interp(x_new, x, y)
    
    # Linear Interpolation
    try:
        linear = interp1d(x, y, kind='linear')
        results['linear'] = linear(x_new)
    except Exception as e:
        print(f"Warning: Linear interpolation failed for {compound_id}: {e}")
        results['linear'] = np.interp(x_new, x, y)
    
    # RBF Interpolation (Gaussian basis)
    try:
        rbf = Rbf(x, y, function='gaussian')
        results['rbf'] = rbf(x_new)
    except Exception as e:
        print(f"Warning: RBF interpolation failed for {compound_id}: {e}")
        results['rbf'] = np.interp(x_new, x, y)
    
    # Gaussian Mixture Model (GMM) Interpolation
    try:
        # Prepare data for GMM
        # Combine x and y into a 2D array for GMM
        data_2d = np.column_stack((x, y))
        
        # Standardize the data for better GMM performance
        scaler = StandardScaler()
        data_scaled = scaler.fit_transform(data_2d)
        
        # Determine optimal number of components (1 to min(5, n_points//2))
        n_components_range = range(1, min(6, len(x)//2 + 1))
        best_n_components = 1
        best_bic = np.inf
        
        for n_components in n_components_range:
            if n_components <= len(x):
                gmm = GaussianMixture(n_components=n_components, random_state=42, n_init=10)
                gmm.fit(data_scaled)
                bic = gmm.bic(data_scaled)
                if bic < best_bic:
                    best_bic = bic
                    best_n_components = n_components
        
        # Fit GMM with optimal number of components
        gmm = GaussianMixture(n_components=best_n_components, random_state=42, n_init=10)
        gmm.fit(data_scaled)
        
        # Create new x values for interpolation
        x_new_2d = np.column_stack((x_new, np.zeros_like(x_new)))
        x_new_scaled = scaler.transform(x_new_2d)
        
        # Use GMM to predict y values
        # We'll use the conditional expectation E[y|x] for each component
        gmm_y_pred = np.zeros(len(x_new))
        
        for i, x_val in enumerate(x_new_scaled):
            # For each new x value, predict y using GMM
            x_val_reshaped = x_val.reshape(1, -1)
            
            # Get responsibilities (posterior probabilities) for each component
            responsibilities = gmm.predict_proba(x_val_reshaped)[0]
            
            # For each component, calculate the conditional expectation
            y_pred_components = []
            for j in range(best_n_components):
                # Get the mean and covariance for this component
                mean = gmm.means_[j]
                cov = gmm.covariances_[j]
                
                # Calculate conditional mean E[y|x] for this component
                # Using the formula: E[y|x] = μ_y + Σ_yx * Σ_xx^(-1) * (x - μ_x)
                mu_x, mu_y = mean[0], mean[1]
                sigma_xx = cov[0, 0]
                sigma_xy = cov[0, 1]
                sigma_yx = cov[1, 0]
                sigma_yy = cov[1, 1]
                
                if sigma_xx > 1e-10:  # Avoid division by zero
                    conditional_mean = mu_y + (sigma_yx / sigma_xx) * (x_val[0] - mu_x)
                    y_pred_components.append(conditional_mean)
                else:
                    y_pred_components.append(mu_y)
            
            # Weight the predictions by component responsibilities
            gmm_y_pred[i] = np.sum(responsibilities * np.array(y_pred_components))
        
        # Transform back to original scale
        gmm_y_pred_2d = np.column_stack((x_new, gmm_y_pred))
        gmm_y_pred_original = scaler.inverse_transform(gmm_y_pred_2d)[:, 1]
        
        results['gmm'] = gmm_y_pred_original
        
        print(f"GMM interpolation for {compound_id}: {best_n_components} components, BIC: {best_bic:.2f}")
        
    except Exception as e:
        print(f"Warning: GMM interpolation failed for {compound_id}: {e}")
        results['gmm'] = np.interp(x_new, x, y)
    
    df = pd.DataFrame(results)
    df.insert(0, 'compound_id', compound_id)
    return df

def verify_interpolation_quality(original_x, original_y, interpolated_df, compound_id, save_path=None):
    """
    Verification technique: Use interpolated points to reconstruct original points and compare.
    
    Parameters:
    - original_x: original wavelength points
    - original_y: original intensity points  
    - interpolated_df: DataFrame with interpolated results (x+y points)
    - compound_id: identifier for the compound
    - save_path: optional path to save the verification plot
    
    Returns: Dictionary with verification metrics and plot path
    """
    original_x = np.array(original_x)
    original_y = np.array(original_y)
    
    # Get interpolated data
    interpolated_x = interpolated_df['wavelength'].values
    methods = ['cubic_spline', 'akima_spline', 'linear', 'rbf', 'gmm']
    
    verification_metrics = {}
    
    for idx, method in enumerate(methods):
        if method not in interpolated_df.columns:
            continue
            
        interpolated_y = interpolated_df[method].values
        
        # Use interpolated points to reconstruct original points
        # We'll use the same interpolation method to go back
        try:
            # Ensure original_x is within the interpolated range
            x_min, x_max = min(interpolated_x), max(interpolated_x)
            valid_original_x = original_x[(original_x >= x_min) & (original_x <= x_max)]
            valid_original_y = original_y[(original_x >= x_min) & (original_x <= x_max)]
            
            if len(valid_original_x) == 0:
                print(f"Warning: No valid original points within interpolated range for {method} in {compound_id}")
                continue
                
            if method == 'cubic_spline':
                # Use cubic spline to interpolate back to original x points
                cs_back = CubicSpline(interpolated_x, interpolated_y)
                reconstructed_y = cs_back(valid_original_x)
            elif method == 'akima_spline':
                akima_back = Akima1DInterpolator(interpolated_x, interpolated_y)
                reconstructed_y = akima_back(valid_original_x)
            elif method == 'linear':
                linear_back = interp1d(interpolated_x, interpolated_y, kind='linear')
                reconstructed_y = linear_back(valid_original_x)
            elif method == 'rbf':
                rbf_back = Rbf(interpolated_x, interpolated_y, function='gaussian')
                reconstructed_y = rbf_back(valid_original_x)
            elif method == 'gmm':
                # For GMM, we'll use a simplified approach since we can't easily reconstruct the GMM
                # Use linear interpolation as fallback for GMM reconstruction
                reconstructed_y = np.interp(valid_original_x, interpolated_x, interpolated_y)
        except Exception as e:
            print(f"Warning: Reconstruction failed for {method} in {compound_id}: {e}")
            # Fallback to simple linear interpolation
            reconstructed_y = np.interp(valid_original_x, interpolated_x, interpolated_y)
        
        # Calculate verification metrics
        mse = np.mean((valid_original_y - reconstructed_y) ** 2)
        mae = np.mean(np.abs(valid_original_y - reconstructed_y))
        max_error = np.max(np.abs(valid_original_y - reconstructed_y))
        correlation = np.corrcoef(valid_original_y, reconstructed_y)[0, 1]
        
        verification_metrics[method] = {
            'mse': mse,
            'mae': mae,
            'max_error': max_error,
            'correlation': correlation,
            'reconstructed_y': reconstructed_y,
            'original_x': valid_original_x,
            'original_y': valid_original_y,
            'interpolated_x': interpolated_x,
            'interpolated_y': interpolated_y
        }
    
    # Create plot data for template display
    plot_data = {
        'original_x': original_x.tolist(),
        'original_y': original_y.tolist(),
        'methods': {}
    }
    
    for method, metrics in verification_metrics.items():
        plot_data['methods'][method] = {
            'interpolated_x': metrics['interpolated_x'].tolist(),
            'interpolated_y': metrics['interpolated_y'].tolist(),
            'reconstructed_x': metrics['original_x'].tolist(),
            'reconstructed_y': metrics['reconstructed_y'].tolist(),
            'metrics': {
                'mse': metrics['mse'],
                'mae': metrics['mae'],
                'max_error': metrics['max_error'],
                'correlation': metrics['correlation']
            }
        }
    
    return {
        'verification_metrics': verification_metrics,
        'plot_data': plot_data,
        'original_points': len(original_x),
        'interpolated_points': len(interpolated_x),
        'additional_points': len(interpolated_x) - len(original_x)
    }

def verify_interpolation_quality_enhanced(original_x, original_y, interpolated_df, compound_id, test_ratio=0.3, save_path=None):
    """
    Enhanced verification technique: Remove some original points and test prediction accuracy.
    
    Parameters:
    - original_x: original wavelength points
    - original_y: original intensity points  
    - interpolated_df: DataFrame with interpolated results (x+y points)
    - compound_id: identifier for the compound
    - test_ratio: fraction of original points to remove for testing (default 0.3 = 30%)
    - save_path: optional path to save the verification plot
    
    Returns: Dictionary with verification metrics and plot path
    """
    original_x = np.array(original_x)
    original_y = np.array(original_y)
    
    # Get interpolated data
    interpolated_x = interpolated_df['wavelength'].values
    methods = ['cubic_spline', 'akima_spline', 'linear', 'rbf', 'gmm']
    
    verification_metrics = {}
    
    # Create training and test sets
    n_points = len(original_x)
    n_test = max(1, int(n_points * test_ratio))  # At least 1 test point
    
    # Randomly select test points
    np.random.seed(42)  # For reproducible results
    test_indices = np.random.choice(n_points, n_test, replace=False)
    train_indices = np.array([i for i in range(n_points) if i not in test_indices])
    
    # Split data
    train_x = original_x[train_indices]
    train_y = original_y[train_indices]
    test_x = original_x[test_indices]
    test_y = original_y[test_indices]
    
    print(f"Enhanced verification for {compound_id}: {len(train_x)} training points, {len(test_x)} test points")
    
    for idx, method in enumerate(methods):
        if method not in interpolated_df.columns:
            continue
            
        interpolated_y = interpolated_df[method].values
        
        # Use interpolated points to reconstruct test points
        try:
            # Ensure test_x is within the interpolated range
            x_min, x_max = min(interpolated_x), max(interpolated_x)
            valid_test_x = test_x[(test_x >= x_min) & (test_x <= x_max)]
            valid_test_y = test_y[(test_x >= x_min) & (test_x <= x_max)]
            
            if len(valid_test_x) == 0:
                print(f"Warning: No valid test points within interpolated range for {method} in {compound_id}")
                continue
                
            if method == 'cubic_spline':
                # Use cubic spline to interpolate back to test x points
                cs_back = CubicSpline(interpolated_x, interpolated_y)
                reconstructed_y = cs_back(valid_test_x)
            elif method == 'akima_spline':
                akima_back = Akima1DInterpolator(interpolated_x, interpolated_y)
                reconstructed_y = akima_back(valid_test_x)
            elif method == 'linear':
                linear_back = interp1d(interpolated_x, interpolated_y, kind='linear')
                reconstructed_y = linear_back(valid_test_x)
            elif method == 'rbf':
                rbf_back = Rbf(interpolated_x, interpolated_y, function='gaussian')
                reconstructed_y = rbf_back(valid_test_x)
            elif method == 'gmm':
                # For GMM, we'll use a simplified approach since we can't easily reconstruct the GMM
                # Use linear interpolation as fallback for GMM reconstruction
                reconstructed_y = np.interp(valid_test_x, interpolated_x, interpolated_y)
        except Exception as e:
            print(f"Warning: Reconstruction failed for {method} in {compound_id}: {e}")
            # Fallback to simple linear interpolation
            reconstructed_y = np.interp(valid_test_x, interpolated_x, interpolated_y)
        
        # Calculate verification metrics
        mse = np.mean((valid_test_y - reconstructed_y) ** 2)
        mae = np.mean(np.abs(valid_test_y - reconstructed_y))
        max_error = np.max(np.abs(valid_test_y - reconstructed_y))
        correlation = np.corrcoef(valid_test_y, reconstructed_y)[0, 1]
        
        verification_metrics[method] = {
            'mse': mse,
            'mae': mae,
            'max_error': max_error,
            'correlation': correlation,
            'reconstructed_y': reconstructed_y,
            'test_x': valid_test_x,
            'test_y': valid_test_y,
            'train_x': train_x,
            'train_y': train_y,
            'interpolated_x': interpolated_x,
            'interpolated_y': interpolated_y
        }
    
    # Create plot data for template display
    plot_data = {
        'original_x': original_x.tolist(),
        'original_y': original_y.tolist(),
        'train_x': train_x.tolist(),
        'train_y': train_y.tolist(),
        'test_x': test_x.tolist(),
        'test_y': test_y.tolist(),
        'methods': {}
    }
    
    for method, metrics in verification_metrics.items():
        plot_data['methods'][method] = {
            'interpolated_x': metrics['interpolated_x'].tolist(),
            'interpolated_y': metrics['interpolated_y'].tolist(),
            'reconstructed_x': metrics['test_x'].tolist(),
            'reconstructed_y': metrics['reconstructed_y'].tolist(),
            'metrics': {
                'mse': metrics['mse'],
                'mae': metrics['mae'],
                'max_error': metrics['max_error'],
                'correlation': metrics['correlation']
            }
        }
    
    return {
        'verification_metrics': verification_metrics,
        'plot_data': plot_data,
        'original_points': len(original_x),
        'train_points': len(train_x),
        'test_points': len(test_x),
        'interpolated_points': len(interpolated_x),
        'additional_points': len(interpolated_x) - len(original_x)
    }

def verify_interpolation_quality_generated_only(original_x, original_y, interpolated_df, compound_id, save_path=None):
    """
    Verification technique: Use ONLY the generated points (not original points) to predict original points.
    
    Parameters:
    - original_x: original wavelength points
    - original_y: original intensity points  
    - interpolated_df: DataFrame with interpolated results (x+y points)
    - compound_id: identifier for the compound
    - save_path: optional path to save the verification plot
    
    Returns: Dictionary with verification metrics and plot path
    """
    original_x = np.array(original_x)
    original_y = np.array(original_y)
    
    # Get interpolated data
    interpolated_x = interpolated_df['wavelength'].values
    interpolated_y_full = interpolated_df['cubic_spline'].values  # Use one method for now
    
    # Find the generated points (points that are NOT in the original data)
    # These are the additional points created by interpolation
    original_x_set = set(original_x)
    generated_indices = []
    
    for i, x_val in enumerate(interpolated_x):
        if x_val not in original_x_set:
            generated_indices.append(i)
    
    generated_x = interpolated_x[generated_indices]
    generated_y = interpolated_y_full[generated_indices]
    
    print(f"Generated points verification for {compound_id}:")
    print(f"  Original points: {len(original_x)}")
    print(f"  Generated points: {len(generated_x)}")
    print(f"  Total interpolated points: {len(interpolated_x)}")
    
    # Debug: Check if we have enough generated points for Akima spline
    if len(generated_x) < 4:
        print(f"Warning: Insufficient generated points ({len(generated_x)}) for Akima spline in {compound_id}")
    
    methods = ['cubic_spline', 'akima_spline', 'linear', 'rbf', 'gmm']
    verification_metrics = {}
    
    for idx, method in enumerate(methods):
        if method not in interpolated_df.columns:
            continue
            
        # Get the generated points for this method
        interpolated_y_method = interpolated_df[method].values
        generated_y_method = interpolated_y_method[generated_indices]
        
        # Debug: Check for NaN values in generated data
        if np.any(np.isnan(generated_y_method)):
            print(f"Warning: NaN values found in generated_y_method for {method} in {compound_id}")
            print(f"  Generated y range: {np.nanmin(generated_y_method)} to {np.nanmax(generated_y_method)}")
        
        # Use ONLY generated points to predict original points
        try:
            if method == 'cubic_spline':
                # Use cubic spline on generated points to predict original points
                cs_generated = CubicSpline(generated_x, generated_y_method)
                predicted_y = cs_generated(original_x)
            elif method == 'akima_spline':
                # Debug: Check if we have enough points and no NaN values
                if len(generated_x) < 4:
                    print(f"Error: Akima spline requires at least 4 points, but only {len(generated_x)} generated points available")
                    raise ValueError("Insufficient points for Akima spline")
                
                if np.any(np.isnan(generated_y_method)):
                    print(f"Error: NaN values in generated_y_method for Akima spline")
                    raise ValueError("NaN values in data")
                
                print(f"Debug: Creating Akima spline with {len(generated_x)} points")
                print(f"  Generated x range: {np.min(generated_x)} to {np.max(generated_x)}")
                print(f"  Generated y range: {np.min(generated_y_method)} to {np.max(generated_y_method)}")
                print(f"  Original x range: {np.min(original_x)} to {np.max(original_x)}")
                
                akima_generated = Akima1DInterpolator(generated_x, generated_y_method)
                predicted_y = akima_generated(original_x)
                
                # Debug: Check if prediction contains NaN
                if np.any(np.isnan(predicted_y)):
                    print(f"Warning: NaN values in Akima spline prediction for {compound_id}")
                    print(f"  Predicted y range: {np.nanmin(predicted_y)} to {np.nanmax(predicted_y)}")
            elif method == 'linear':
                linear_generated = interp1d(generated_x, generated_y_method, kind='linear')
                predicted_y = linear_generated(original_x)
            elif method == 'rbf':
                rbf_generated = Rbf(generated_x, generated_y_method, function='gaussian')
                predicted_y = rbf_generated(original_x)
            elif method == 'gmm':
                # For GMM, we'll use a simplified approach since we can't easily reconstruct the GMM
                # Use linear interpolation as fallback for GMM prediction
                predicted_y = np.interp(original_x, generated_x, generated_y_method)
        except Exception as e:
            print(f"Warning: Prediction failed for {method} in {compound_id}: {e}")
            # Fallback to simple linear interpolation
            predicted_y = np.interp(original_x, generated_x, generated_y_method)
        
        # Check for NaN values in prediction
        if np.any(np.isnan(predicted_y)):
            print(f"Warning: NaN values in predicted_y for {method} in {compound_id}")
            # Replace NaN values with interpolated values
            predicted_y = np.where(np.isnan(predicted_y), 
                                 np.interp(original_x, generated_x, generated_y_method), 
                                 predicted_y)
        
        # Calculate verification metrics
        mse = np.mean((original_y - predicted_y) ** 2)
        mae = np.mean(np.abs(original_y - predicted_y))
        max_error = np.max(np.abs(original_y - predicted_y))
        correlation = np.corrcoef(original_y, predicted_y)[0, 1]
        
        # Debug: Check for NaN in metrics
        if np.isnan(mse) or np.isnan(mae) or np.isnan(max_error) or np.isnan(correlation):
            print(f"Warning: NaN values in metrics for {method} in {compound_id}")
            print(f"  MSE: {mse}, MAE: {mae}, Max Error: {max_error}, Correlation: {correlation}")
        
        verification_metrics[method] = {
            'mse': mse,
            'mae': mae,
            'max_error': max_error,
            'correlation': correlation,
            'predicted_y': predicted_y,
            'original_x': original_x,
            'original_y': original_y,
            'generated_x': generated_x,
            'generated_y': generated_y_method,
            'interpolated_x': interpolated_x,
            'interpolated_y': interpolated_y_method
        }
    
    # Create plot data for template display
    plot_data = {
        'original_x': original_x.tolist(),
        'original_y': original_y.tolist(),
        'generated_x': generated_x.tolist(),
        'generated_y': generated_y.tolist(),
        'methods': {}
    }
    
    for method, metrics in verification_metrics.items():
        plot_data['methods'][method] = {
            'interpolated_x': metrics['interpolated_x'].tolist(),
            'interpolated_y': metrics['interpolated_y'].tolist(),
            'predicted_x': metrics['original_x'].tolist(),
            'predicted_y': metrics['predicted_y'].tolist(),
            'metrics': {
                'mse': float(metrics['mse']),
                'mae': float(metrics['mae']),
                'max_error': float(metrics['max_error']),
                'correlation': float(metrics['correlation'])
            }
        }
    
    return {
        'verification_metrics': verification_metrics,
        'plot_data': plot_data
    }

def create_verification_summary(verification_results):
    """
    Create a summary of verification results across all methods.
    
    Parameters:
    - verification_results: Dictionary with verification metrics
    
    Returns: DataFrame with summary statistics
    """
    summary_data = []
    
    for method, metrics in verification_results['verification_metrics'].items():
        summary_data.append({
            'Method': method.replace('_', ' ').title(),
            'MSE': metrics['mse'],
            'MAE': metrics['mae'],
            'Max Error': metrics['max_error'],
            'Correlation': metrics['correlation']
        })
    
    summary_df = pd.DataFrame(summary_data)
    summary_df = summary_df.sort_values('MSE')  # Sort by best performance
    
    return summary_df

# # Rerun with fixed GMM
# x = [400, 420, 440, 460, 480, 500, 520, 540, 560]
# y = [0.1, 0.3, 0.5, 0.7, 1.0, 0.7, 0.5, 0.3, 0.1]
# compound_id = "CMPD001"
# interpolated_df_fixed = interpolate_methods_fixed(x, y, compound_id, step=0.5)
