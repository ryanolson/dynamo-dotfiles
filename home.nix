{ pkgs, lib, ... }:
let 
  username = builtins.getEnv "USER";
  homeDirectory = builtins.getEnv "HOME";
in {
  home = {
    inherit username;
    inherit homeDirectory;
    stateVersion = "25.05";
  };

  imports = [
    # Import team base configuration from GitHub (using specific commit hash)
    "${builtins.fetchTarball {
      url = "https://github.com/ryanolson/dynamo-nix/archive/95cbbf2.tar.gz";
    }}/team-base.nix"
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