# Rust Development Rules for AI Coding Agents

## Code Style & Formatting

### Use Standard Rust Formatting
- **ALWAYS** use `cargo fmt` before committing code
- Follow the default `rustfmt` configuration unless there's a compelling project-specific reason to customize
- **NEVER** manually format code - let `rustfmt` handle all formatting decisions
- Set up your editor to format on save: `cargo fmt` should run automatically

### Import Organization
Organize imports into these groups (separated by blank lines):
1. Standard library imports (`use std::...`)
2. External crate imports (`use serde::...`) 
3. Internal crate imports (`use crate::...`)
4. Relative imports (`use super::...`, `use self::...`)

```rust
use std::collections::HashMap;
use std::fs::File;

use serde::{Deserialize, Serialize};
use tokio::time::Duration;

use crate::models::User;
use crate::utils::helpers;

use super::config;
```

## Code Quality & Linting

### Clippy Integration
- **ALWAYS** run `cargo clippy` and address all warnings before submitting code
- Use `cargo clippy -- -D warnings` in CI to treat warnings as errors
- For specific cases where clippy warnings don't apply, use targeted suppression:
  ```rust
  #[allow(clippy::unnecessary_wraps)]  // Be specific about which lint
  fn example() -> Option<i32> { Some(42) }
  ```
- **NEVER** use broad suppressions like `#[allow(clippy::all)]`

### Quality Checks to Enforce
- No unused variables, imports, or functions
- No unreachable code
- No infinite loops without clear intent
- Prefer iterators over manual loops when possible
- Use meaningful variable and function names (no single letters except for very short scopes)

## Error Handling Best Practices

### Result and Option Types
- **ALWAYS** handle `Result` and `Option` types explicitly
- **NEVER** use `.unwrap()` in production code without justification
- **PREFER** `.expect("clear error message")` over `.unwrap()` when panicking is intentional
- Use `.unwrap()` ONLY when:
  - Writing examples, tests, or prototypes
  - You have a documented invariant that guarantees success
  - The panic indicates a bug in your program logic

### Proper Error Propagation
```rust
// ✅ Good: Use ? operator for error propagation
fn read_config() -> Result<Config, ConfigError> {
    let content = fs::read_to_string("config.toml")?;
    let config: Config = toml::from_str(&content)?;
    Ok(config)
}

// ❌ Bad: Using unwrap in production code
fn read_config() -> Config {
    let content = fs::read_to_string("config.toml").unwrap();
    toml::from_str(&content).unwrap()
}
```

### Error Types
- Create custom error types for your domain using `thiserror` crate
- Use `anyhow` for application-level error handling
- Implement `std::error::Error` trait for custom error types
- Provide meaningful error messages that help users understand what went wrong

## Memory Safety & Ownership

### Smart Pointer Usage
- Prefer owned types (`String`, `Vec<T>`) in function signatures unless borrowing is specifically needed
- Use `&str` and `&[T]` for read-only function parameters
- Use `Rc<T>` and `Arc<T>` only when you need shared ownership
- Prefer `Box<T>` over `Rc<T>` when you don't need sharing

### Borrowing Guidelines
- Keep borrowing scopes as short as possible
- Avoid complex lifetime annotations when possible - redesign the API instead
- Use `Cow<T>` when you might need either borrowed or owned data
- Document lifetime relationships in complex cases

## Performance & Efficiency

### Collection Usage
- Pre-allocate collections when you know the size: `Vec::with_capacity(n)`
- Use `HashMap` for key-value lookups, `BTreeMap` when you need ordering
- Prefer `&str` over `String` for function parameters
- Use string formatting (`format!`, `tracing::debug!`) judiciously - consider if you really need allocation

### Iterator Patterns
```rust
// ✅ Good: Use iterator combinators
let results: Vec<_> = items
    .iter()
    .filter(|item| item.is_valid())
    .map(|item| item.process())
    .collect();

// ❌ Avoid: Manual loops when iterators are clearer
let mut results = Vec::new();
for item in &items {
    if item.is_valid() {
        results.push(item.process());
    }
}
```

## Function Design

### Function Signatures
- Keep functions small and focused on a single responsibility
- Use descriptive function names that clearly indicate what the function does
- Prefer returning `Result<T, E>` for fallible operations
- Use `impl Trait` for return types when appropriate to reduce verbosity

### Parameter Guidelines
- Prefer borrowed parameters (`&str`, `&[T]`) for read-only access
- Use owned parameters (`String`, `Vec<T>`) when the function needs to take ownership
- Group related parameters into structs when you have more than 3-4 parameters
- Use builder pattern for complex construction

## Documentation Standards

### Code Documentation
- Write doc comments for all public APIs using `///`
- Include examples in doc comments when helpful
- Document panics, errors, and safety requirements
- Use `cargo doc` to verify documentation builds correctly

```rust
/// Parses a configuration file from the given path.
///
/// # Arguments
/// * `path` - The path to the configuration file
///
/// # Returns
/// Returns the parsed configuration or an error if parsing fails.
///
/// # Errors
/// This function will return an error if:
/// - The file cannot be read
/// - The file contains invalid TOML syntax
///
/// # Examples
/// ```
/// let config = parse_config("config.toml")?;
/// ```
pub fn parse_config<P: AsRef<Path>>(path: P) -> Result<Config, ConfigError> {
    // implementation
}
```

## Testing Guidelines

### Test Organization
- Place unit tests in the same file using `#[cfg(test)]`
- Create integration tests in the `tests/` directory
- Use descriptive test function names that describe what is being tested
- Follow the pattern: `test_<function>_<scenario>_<expected_result>`

### Test Best Practices
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_config_valid_file_returns_config() {
        // Arrange
        let config_content = r#"
            name = "test"
            version = "1.0"
        "#;
        
        // Act
        let result = parse_config_from_str(config_content);
        
        // Assert
        assert!(result.is_ok());
        let config = result.unwrap();
        assert_eq!(config.name, "test");
        assert_eq!(config.version, "1.0");
    }

    #[test]
    fn test_parse_config_invalid_toml_returns_error() {
        let invalid_content = "invalid toml content [[[";
        let result = parse_config_from_str(invalid_content);
        assert!(result.is_err());
    }
}
```

## Security Considerations

### Safe Code Practices
- **AVOID** `unsafe` code unless absolutely necessary
- When using `unsafe`, document why it's needed and what invariants you're maintaining
- Validate all external inputs (user input, network data, file contents)
- Use typed APIs to prevent common security mistakes
- Be careful with integer overflow - use checked arithmetic when needed

### Dependency Management
- Regularly run `cargo audit` to check for vulnerable dependencies
- Keep dependencies up to date but test thoroughly
- Minimize the number of dependencies when possible
- Review new dependencies for quality and maintenance status

## Module Organization

### Project Structure
- Organize code into logical modules using files and directories
- Use `mod.rs` files to create module hierarchies
- Re-export important types at appropriate levels using `pub use`
- Keep module interfaces clean and minimal

### Visibility Rules
- Make fields and functions private by default
- Use `pub` only when external access is needed
- Consider `pub(crate)` for internal APIs that need to be shared across modules
- Use `pub(super)` for APIs that should only be accessible to parent modules

## Concurrency & Async Code

### Async Guidelines
- Use `async`/`await` for I/O-bound operations
- Prefer `tokio` for async runtime in applications
- Use `Arc<Mutex<T>>` or `Arc<RwLock<T>>` for shared mutable state
- Consider using channels (`mpsc`, `oneshot`) for communication between tasks

### Threading Best Practices
- Use `thread::spawn` sparingly - prefer async for most concurrent work
- Use `std::sync` primitives correctly and understand their performance characteristics
- Avoid shared mutable state when possible - prefer message passing
- Use `rayon` for data parallelism when appropriate

## Cargo and Project Configuration

### Cargo.toml Best Practices
- Always specify the Rust edition (currently `edition = "2021"`)
- Use semantic versioning for your crate versions
- Include appropriate metadata: description, license, repository
- Organize dependencies into `[dependencies]`, `[dev-dependencies]`, and `[build-dependencies]`

### Feature Management
- Use Cargo features to make optional functionality opt-in
- Document feature flags in your README
- Avoid feature creep - keep the core library focused
- Use default features sparingly and document them clearly

## Comments and Code Clarity

### When to Comment
- Explain **why** something is done, not **what** is done (the code should show what)
- Document complex algorithms or business logic
- Explain non-obvious performance optimizations
- Document safety requirements for `unsafe` code
- Add TODO comments for known technical debt with issue numbers when possible

### Code Readability
- Use meaningful variable names that don't require comments
- Break complex expressions into intermediate variables with descriptive names
- Prefer explicit types over `let x = ...` when the type isn't obvious
- Use early returns to reduce nesting levels

## AI-Specific Guidelines

When working with AI coding assistants:

### Code Generation Preferences
- Request implementations that follow these exact guidelines
- Ask for comprehensive error handling using `Result` types
- Prefer explicit, readable code over clever one-liners
- Request documentation and tests along with implementation code

### Review and Refinement
- Always run `cargo fmt` and `cargo clippy` on AI-generated code
- Verify that proper error handling is implemented
- Check that the code follows Rust naming conventions
- Ensure tests are included for new functionality

### Common AI Mistakes to Watch For
- Using `.unwrap()` inappropriately in production code
- Missing error handling in function chains
- Overly complex lifetime annotations that could be simplified
- Not using the most appropriate collection types
- Missing documentation for public APIs

---

## Quick Reference Checklist

Before submitting any Rust code, verify:

- [ ] `cargo fmt` has been run
- [ ] `cargo clippy` passes with no warnings
- [ ] All `Result` and `Option` types are properly handled
- [ ] No `.unwrap()` in production code without justification
- [ ] Public APIs have documentation
- [ ] Tests are included for new functionality
- [ ] Error messages are clear and helpful
- [ ] Code follows the single responsibility principle
- [ ] Imports are organized correctly
- [ ] No unused code or imports

Remember: These rules prioritize **correctness**, **safety**, and **maintainability** over brevity. Rust's type system is your friend - use it to catch bugs at compile time rather than runtime.
