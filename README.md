# Personal Development Environment

Personal dotfiles that extend the Dynamo team base configuration.

## Quick Setup

```bash
curl -fsSL https://raw.githubusercontent.com/yourusername/dynamo-dotfiles/main/bootstrap.sh | bash
```

## What This Adds

This configuration **imports** the [Dynamo team base](../dynamo-nix/README.md) and adds:

- **Personal Git configuration** - Your name, email, and preferences
- **Personal shell aliases** - Your custom shortcuts and commands
- **Personal packages** - Tools specific to your workflow
- **Machine-specific settings** - SSH configs, API keys, etc.

## Configuration Structure

```
dynamo-dotfiles/
├── flake.nix           # Personal flake configuration
├── home.nix            # Personal Home Manager config
├── bootstrap.sh        # Personal setup script
└── README.md           # This file
```

## Customization

### Git Configuration

Edit `home.nix` to set your personal information:

```nix
programs.git = {
  enable = true;
  userName = "Your Name";        # <- Change this
  userEmail = "you@company.com"; # <- Change this
  # ... other git settings
};
```

### Personal Aliases

Add your own shell aliases:

```nix
programs.fish.shellAliases = {
  # Personal shortcuts
  gst = "git status";
  gco = "git checkout";
  # Add your own...
};
```

### Additional Packages

Add tools not in the team base:

```nix
home.packages = with pkgs; [
  # Your personal tools
  # ccmanager  # Custom tools
  # ruler      # May need special handling
];
```

### Environment Variables

Set personal environment variables:

```nix
home.sessionVariables = {
  EDITOR = "hx";  # Default editor
  # Add your own...
};
```

## Team Base Import

This configuration automatically imports the team base from GitHub:

```nix
imports = [
  (builtins.fetchurl {
    url = "https://raw.githubusercontent.com/dynamo/nix/main/team-base.nix";
    sha256 = "...";  # Updated automatically
  })
];
```

When the team base changes, update the SHA256 hash and run:

```bash
home-manager switch --flake github:yourusername/dynamo-dotfiles
```

## Local Development

For testing changes locally:

```bash
# Clone both repositories  
git clone https://github.com/dynamo/nix.git dynamo-nix
git clone https://github.com/yourusername/dynamo-dotfiles.git

# Test with local import (edit home.nix)
imports = [ ../dynamo-nix/team-base.nix ];

# Apply changes
nix run home-manager/release-25.05 -- switch --flake .
```

## Secrets Management

**Don't put secrets in Git!** For sensitive data:

### Option 1: Environment Variables
```nix
programs.git.userEmail = builtins.getEnv "GIT_USER_EMAIL";
```

### Option 2: Separate Private Repository
Keep sensitive configs in a private repo and import them.

### Option 3: Local Files
Store secrets in `~/.local/secrets/` and reference them:
```nix
programs.ssh.extraConfig = builtins.readFile ~/.local/secrets/ssh_config;
```

## Updates

### Update Team Base + Personal Config
```bash
home-manager switch --flake github:yourusername/dynamo-dotfiles
```

### Update Only Personal Changes  
```bash
cd ~/path/to/dynamo-dotfiles
git pull
home-manager switch --flake .
```

## Team vs Personal

| Configuration | Team Base | Personal |
|---------------|-----------|----------|
| Development tools | ✅ | - |
| Language servers | ✅ | - |
| Shell aliases (generic) | ✅ | - |
| Editor configs | ✅ | Override |
| Git user info | - | ✅ |
| SSH keys | - | ✅ |
| Personal aliases | - | ✅ |
| API keys/tokens | - | ✅ |

## Troubleshooting

### Team base import failing
Check that the team repository is accessible and the SHA256 hash is correct.

### Personal overrides not working  
Make sure your personal config comes **after** the team base import in the `imports` list.

### Git config not applied
Verify your personal git configuration is in the right section of `home.nix`.