{ pkgs, ... }: {
  home = {
    username = "rolson";  # Update this to your username
    homeDirectory = "/Users/rolson";  # Update this to your home directory
    stateVersion = "25.05";
  };

  imports = [
    # Import team base configuration from GitHub
    (builtins.fetchurl {
      url = "https://raw.githubusercontent.com/ryanolson/dynamo-nix/main/team-base.nix";
      sha256 = "sha256-0000000000000000000000000000000000000000000000000000";  # Will be updated after first push
    })
  ];
  
  # Personal git configuration (not in team base)
  programs.git = {
    enable = true;
    userName = "Your Name";        # Replace with your name
    userEmail = "you@company.com"; # Replace with your email
    extraConfig = {
      init.defaultBranch = "main";
      pull.rebase = true;
      push.autoSetupRemote = true;
    };
  };
  
  # Personal shell aliases (in addition to team base)
  programs.fish.shellAliases = {
    # Personal shortcuts - add your own here
    gst = "git status";
    gco = "git checkout";
    gp = "git push";
    gl = "git pull";
    ga = "git add";
    gc = "git commit";
    
    # Personal machine-specific aliases
    # hq = "ssh phendricks@10.110.21.9";  # Example from your config
  };
  
  # Personal packages (in addition to team base)
  home.packages = with pkgs; [
    # Add any personal tools here that aren't in the team base
    # Examples:
    # ccmanager  # Not in nixpkgs - may need custom installation
    # ruler      # Not in nixpkgs - may need custom installation
  ];
  
  # Personal environment variables
  home.sessionVariables = {
    EDITOR = "hx";  # Set default editor to helix
    # Add other personal environment variables here
  };
}